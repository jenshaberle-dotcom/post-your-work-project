from __future__ import annotations

import base64
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
REPOSITORY = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
APPLY = os.environ.get("DRJ_APPLY", "false").lower() == "true"
MAX_EFFECTS = int(os.environ.get("DRJ_MAX_EFFECTS", "250"))
OUTCOME_PATH = Path(os.environ.get("DRJ_OUTCOME_PATH", "/tmp/drj-branch-domain-outcome.json"))

MANAGED = {
    "jenshaberle-dotcom/my-travel-plans": 1206902456,
    "jenshaberle-dotcom/course-collaboration-travel-plans": 1209268162,
    "jenshaberle-dotcom/lighthouse": 1209288077,
    "jenshaberle-dotcom/post-your-work-project": 1210268492,
    "jenshaberle-dotcom/nd081-c1-exercises": 1212442488,
    "jenshaberle-dotcom/Azure-Applications-project": 1223344327,
    "jenshaberle-dotcom/mcp-autonomous-engineering-agent": 1269463183,
    "jenshaberle-dotcom/current-known-good-baseline": 1276271604,
    "jenshaberle-dotcom/pedestrian-dataset-engineering": 1278332163,
    "jenshaberle-dotcom/Azure-Data-Warehouse-Project": 1285081970,
    "jenshaberle-dotcom/phone-wake-relay": 1319247303,
    "jenshaberle-dotcom/Projekt-Novi": 1324480881,
    "jenshaberle-dotcom/NOVI-Children-of-the-Deep": 1328746404,
    "jenshaberle-dotcom/Worker-based-agent-architecture": 1331755620,
    "jenshaberle-dotcom/Experiment-vault": 1331915282,
    "jenshaberle-dotcom/Data-Retention-Janitor": 1347048382,
}
NO_TOUCH = {
    "jenshaberle-dotcom/Runner-Control-Center---RCC",
    "jenshaberle-dotcom/job-application-pipeline",
    "jenshaberle-dotcom/job-pipeline-runtime",
}
SENSITIVE_PREFIXES = ("archive/", "release/")
EXPLICIT_PRESERVE_DISPOSITIONS = {"KEEP", "ACTIVE", "PRESERVE", "HARVEST_PENDING", "REVIEW"}
EXPLICIT_RETIRE_DISPOSITIONS = {"RETIRE", "SUPERSEDED", "DEPRECATED"}
UNRESOLVED_BLOCKING_STATES = {"UNKNOWN", "REVIEW", "HARVEST_PENDING", "PRESERVE", "AMBIGUOUS"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class DrjError(RuntimeError):
    pass


def api(method: str, path: str, *, payload: dict[str, Any] | None = None, allow_404: bool = False) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{API_ROOT}/repos/{REPOSITORY}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "drj-portable-branch-domain-janitor-v1",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else None
    except HTTPError as exc:
        if allow_404 and exc.code == 404:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise DrjError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc


def paged(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    separator = "&" if "?" in path else "?"
    while True:
        payload = api("GET", f"{path}{separator}per_page=100&page={page}")
        items = list(payload or [])
        out.extend(items)
        if len(items) < 100:
            return out
        page += 1
        if page > 20:
            raise DrjError(f"pagination bound exceeded for {path}")


def root_json(path: str) -> dict[str, Any] | None:
    encoded = quote(path, safe="/")
    payload = api("GET", f"/contents/{encoded}", allow_404=True)
    if payload is None:
        return None
    if payload.get("type") != "file" or payload.get("encoding") != "base64":
        raise DrjError(f"unsupported root contract response for {path}")
    raw = base64.b64decode(str(payload["content"]))
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise DrjError(f"{path} must contain a JSON object")
    return value


def parse_not_before(candidate: dict[str, Any]) -> datetime | None:
    value = candidate.get("not_before_utc")
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise DrjError(f"naive not_before_utc for {candidate.get('identity')}")
    return parsed.astimezone(UTC)


def parse_branch_identity(identity: str) -> tuple[str | None, str | None]:
    raw = identity.strip()
    if raw.startswith("refs/heads/"):
        raw = raw[len("refs/heads/"):]
    if raw.startswith("scope:") or not raw:
        return None, None
    expected_sha: str | None = None
    if "@" in raw:
        candidate_branch, candidate_sha = raw.rsplit("@", 1)
        if _SHA_RE.fullmatch(candidate_sha):
            raw = candidate_branch
            expected_sha = candidate_sha
    return raw, expected_sha


def open_pr_block(branch: str) -> bool:
    owner = REPOSITORY.split("/", 1)[0]
    head_query = urlencode({"state": "open", "head": f"{owner}:{branch}", "per_page": "1"})
    base_query = urlencode({"state": "open", "base": branch, "per_page": "1"})
    return bool(api("GET", f"/pulls?{head_query}") or api("GET", f"/pulls?{base_query}"))


def collect_mailbox_state(mailbox: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, datetime], bool, bool]:
    dispositions: dict[str, str] = {}
    expected_shas: dict[str, str] = {}
    not_before: dict[str, datetime] = {}

    for candidate in mailbox.get("candidates") or []:
        if not isinstance(candidate, dict) or candidate.get("target_type") != "REMOTE_BRANCH":
            continue
        identity = candidate.get("identity")
        if not isinstance(identity, str):
            continue
        branch, identity_sha = parse_branch_identity(identity)
        if branch is None:
            continue
        disposition = str(candidate.get("semantic_disposition") or "").upper()
        if disposition:
            dispositions[branch] = disposition
        explicit_sha = candidate.get("expected_sha")
        if isinstance(explicit_sha, str) and _SHA_RE.fullmatch(explicit_sha):
            expected_shas[branch] = explicit_sha
        elif identity_sha:
            expected_shas[branch] = identity_sha
        boundary = parse_not_before(candidate)
        if boundary is not None:
            not_before[branch] = boundary

    for item in mailbox.get("preserve_or_review") or []:
        if not isinstance(item, dict) or item.get("target_type") != "REMOTE_BRANCH":
            continue
        identity = item.get("identity")
        if not isinstance(identity, str):
            continue
        branch, identity_sha = parse_branch_identity(identity)
        if branch is None:
            continue
        dispositions[branch] = "PRESERVE"
        explicit_sha = item.get("expected_sha")
        if isinstance(explicit_sha, str) and _SHA_RE.fullmatch(explicit_sha):
            expected_shas[branch] = explicit_sha
        elif identity_sha:
            expected_shas[branch] = identity_sha

    unresolved_remote_scope = False
    for item in mailbox.get("unresolved") or []:
        if not isinstance(item, dict) or item.get("target_type") != "REMOTE_BRANCH":
            continue
        identity = str(item.get("identity") or "")
        state = str(item.get("state") or "").upper()
        if identity.startswith("scope:") and state in UNRESOLVED_BLOCKING_STATES:
            unresolved_remote_scope = True
            break

    desired = mailbox.get("desired_state") or {}
    remote_default = str(
        desired.get("remote_non_candidate_default")
        or desired.get("remote_non_main_default")
        or ""
    ).upper()
    generic_zero_ahead_allowed = not unresolved_remote_scope and not any(
        marker in remote_default for marker in ("PRESERVE", "REVIEW")
    )
    return dispositions, expected_shas, not_before, unresolved_remote_scope, generic_zero_ahead_allowed


def main() -> None:
    if REPOSITORY in NO_TOUCH:
        raise DrjError(f"NO_TOUCH repository rejected: {REPOSITORY}")
    if REPOSITORY not in MANAGED:
        raise DrjError(f"repository is not in managed branch-hygiene allowlist: {REPOSITORY}")

    repository = api("GET", "")
    repo_id = int(repository["id"])
    if repo_id != MANAGED[REPOSITORY]:
        raise DrjError(f"repository identity mismatch: expected={MANAGED[REPOSITORY]} observed={repo_id}")
    default_branch = str(repository["default_branch"])

    project_drj = root_json("PROJECT-DRJ.json")
    if project_drj is None:
        raise DrjError("PROJECT-DRJ.json is required before Branch Domain Janitor effects")
    authority = project_drj.get("authority") or {}
    if authority.get("project_local_branch_delete_authority") not in (False, None):
        raise DrjError("project-local branch delete authority must not be enabled")
    if authority.get("orchestrator_delete_authority") is not False:
        raise DrjError("orchestrator_delete_authority must be false")

    mailbox = root_json("DRJ-RECONCILE-REQUEST.json") or {}
    persistent = {default_branch}
    desired = mailbox.get("desired_state") or {}
    for item in desired.get("persistent_refs") or []:
        if isinstance(item, str) and item:
            persistent.add(item)

    explicit_disposition, expected_shas, not_before, unresolved_remote_scope, generic_zero_ahead_allowed = collect_mailbox_state(mailbox)

    open_pulls = paged("/pulls?state=open")
    live_refs: set[str] = set()
    for pull in open_pulls:
        head = (pull.get("head") or {}).get("ref")
        base = (pull.get("base") or {}).get("ref")
        if isinstance(head, str):
            live_refs.add(head)
        if isinstance(base, str):
            live_refs.add(base)

    branches = paged("/branches")
    now = datetime.now(UTC)
    outcomes: list[dict[str, Any]] = []
    effects = 0

    for branch in sorted(branches, key=lambda item: str(item.get("name", ""))):
        name = str(branch["name"])
        observed_sha = str((branch.get("commit") or {})["sha"])
        protected = bool(branch.get("protected", False))
        disposition = explicit_disposition.get(name, "")
        expected_sha = expected_shas.get(name)
        explicit_retire = disposition in EXPLICIT_RETIRE_DISPOSITIONS
        outcome: dict[str, Any] = {
            "branch": name,
            "observed_sha": observed_sha,
            "explicit_disposition": disposition or None,
            "expected_candidate_sha": expected_sha,
        }

        if name == default_branch:
            outcome["decision"] = "KEEP_DEFAULT"
        elif name in persistent:
            outcome["decision"] = "KEEP_PERSISTENT"
        elif name in live_refs:
            outcome["decision"] = "KEEP_LIVE_PR"
        elif protected:
            outcome["decision"] = "KEEP_GITHUB_PROTECTED"
        elif disposition in EXPLICIT_PRESERVE_DISPOSITIONS:
            outcome["decision"] = f"KEEP_EXPLICIT_{disposition}"
        elif name.startswith(SENSITIVE_PREFIXES):
            outcome["decision"] = "PRESERVE_SENSITIVE_NAMESPACE"
        elif name in not_before and now < not_before[name]:
            outcome["decision"] = "BLOCKED_NOT_BEFORE"
            outcome["not_before_utc"] = not_before[name].isoformat()
        elif expected_sha and expected_sha != observed_sha:
            outcome["decision"] = "BLOCKED_CANDIDATE_SHA_MISMATCH"
        elif not explicit_retire and not generic_zero_ahead_allowed:
            outcome["decision"] = "KEEP_PROJECT_REMOTE_DEFAULT"
        else:
            compare = api("GET", f"/compare/{quote(default_branch, safe='')}...{quote(name, safe='')}")
            ahead = int(compare["ahead_by"])
            behind = int(compare["behind_by"])
            outcome["ahead_by"] = ahead
            outcome["behind_by"] = behind
            if ahead != 0:
                outcome["decision"] = "HARVEST_PENDING_UNIQUE_COMMITS"
            elif effects >= MAX_EFFECTS:
                outcome["decision"] = "ELIGIBLE_DEFERRED_EFFECT_BOUND"
            elif not APPLY:
                outcome["decision"] = "DRY_RUN_RETIRE_ELIGIBLE_ZERO_AHEAD"
            else:
                ref_path = quote(f"heads/{name}", safe="")
                fresh = api("GET", f"/git/ref/{ref_path}", allow_404=True)
                if fresh is None:
                    outcome["decision"] = "ALREADY_ABSENT"
                else:
                    fresh_sha = str((fresh.get("object") or {})["sha"])
                    if fresh_sha != observed_sha:
                        raise DrjError(f"ref moved during reconciliation: {name} observed={observed_sha} fresh={fresh_sha}")
                    if expected_sha and expected_sha != fresh_sha:
                        raise DrjError(f"candidate SHA changed during reconciliation: {name} expected={expected_sha} fresh={fresh_sha}")
                    if open_pr_block(name):
                        outcome["decision"] = "KEEP_LIVE_PR_REVALIDATED"
                    else:
                        fresh_mailbox = root_json("DRJ-RECONCILE-REQUEST.json") or {}
                        fresh_dispositions, fresh_expected, _, _, fresh_generic_allowed = collect_mailbox_state(fresh_mailbox)
                        fresh_disposition = fresh_dispositions.get(name, "")
                        if fresh_disposition in EXPLICIT_PRESERVE_DISPOSITIONS:
                            raise DrjError(f"branch became explicitly preserved during reconciliation: {name} disposition={fresh_disposition}")
                        if fresh_disposition not in EXPLICIT_RETIRE_DISPOSITIONS and not fresh_generic_allowed:
                            raise DrjError(f"branch became blocked by project remote default during reconciliation: {name}")
                        if fresh_expected.get(name) and fresh_expected[name] != fresh_sha:
                            raise DrjError(f"fresh candidate SHA mismatch during reconciliation: {name}")
                        recompare = api("GET", f"/compare/{quote(default_branch, safe='')}...{quote(name, safe='')}")
                        if int(recompare["ahead_by"]) != 0:
                            raise DrjError(f"branch gained unique commits during reconciliation: {name}")
                        api("DELETE", f"/git/refs/{ref_path}")
                        if api("GET", f"/git/ref/{ref_path}", allow_404=True) is not None:
                            raise DrjError(f"post-effect ref verification failed: {name}")
                        effects += 1
                        outcome["decision"] = "RETIRED_ZERO_AHEAD"
                        outcome["effect_index"] = effects
        outcomes.append(outcome)

    counts: dict[str, int] = {}
    for item in outcomes:
        key = str(item["decision"])
        counts[key] = counts.get(key, 0) + 1

    receipt = {
        "schema_version": "drj.portable_branch_domain_janitor.outcome.v1",
        "repository": REPOSITORY,
        "repository_id": repo_id,
        "default_branch": default_branch,
        "observed_at": now.isoformat(),
        "apply": APPLY,
        "max_effects": MAX_EFFECTS,
        "unresolved_remote_scope": unresolved_remote_scope,
        "generic_zero_ahead_allowed": generic_zero_ahead_allowed,
        "effects_executed": effects,
        "counts": counts,
        "outcomes": outcomes,
    }
    OUTCOME_PATH.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: receipt[key] for key in ("repository", "default_branch", "apply", "effects_executed", "unresolved_remote_scope", "generic_zero_ahead_allowed", "counts")}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
