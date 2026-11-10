"""只比较实际输出与已签基准；不参与评分，也不自行批准语义质量。"""
import hashlib
import json
import math
from itertools import combinations
from pathlib import Path


def load_benchmark(samples: Path, version: str = "v2.1") -> dict:
    if version not in {"v2", "v2.1"}:
        raise ValueError("不支持的基准版本")
    manifest = json.loads((samples / f"manifest-{version}.json").read_text(encoding="utf-8"))
    for filename, digest in manifest["files"].items():
        if hashlib.sha256((samples / filename).read_bytes()).hexdigest() != digest:
            raise ValueError(f"冻结文件已变化：{filename}")
    baseline = json.loads((samples / f"annotations-{version}.json").read_text(encoding="utf-8"))
    if baseline["version"] != f"demo-{version}" or manifest["version"] != baseline["version"] or manifest["scoring_rules_version"] != baseline["scoring_rules_version"]:
        raise ValueError("基准版本与冻结清单不一致")
    if baseline.get("human_signoff") is not True or len(baseline["pairs"]) != 24 or any(
        pair.get("human_signoff") is not True for pair in baseline["pairs"]
    ):
        raise ValueError("24组基准尚未全部签收")
    baseline["jd_texts"] = {key: (samples / job["source_file"]).read_text(encoding="utf-8")
                            for key, job in baseline["jobs"].items()}
    return baseline


def _indexed(rows, key):
    result = {key(row): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("实际结果含重复配对、维度或条款，不能覆盖后静默比较")
    return result


def compare_results(baseline: dict, results: list[dict]) -> dict:
    key = lambda row: (row["sample_id"], row["job_id"])
    expected = _indexed(baseline["pairs"], key)
    actual = _indexed(results, key)
    if expected.keys() != actual.keys():
        raise ValueError("实际结果配对缺失或多余，必须完整提供24组")
    pairs = []
    for pair_key, target in expected.items():
        row = actual[pair_key]
        snapshot = row.get("input_snapshot") or {}
        catalog = snapshot.get("evidence_catalog") or {}
        dimensions = _indexed(row["dimensions"], lambda item: item["name"])
        entries = _indexed([c for dim in row["dimensions"] for c in dim["criteria"]], lambda c: c["id"])
        expected_ids = {c["id"] for c in target["criteria"]}
        criterion_set_valid = set(entries) == expected_ids and all(
            {c["id"] for c in dim["criteria"]} == {identifier for identifier in expected_ids if identifier.startswith(name + ":")}
            for name, dim in dimensions.items())
        checks = []
        for criterion in target["criteria"]:
            observed = entries.get(criterion["id"], {})
            references = observed.get("references") or []
            needs_reference = observed.get("score", 0) > 0 or observed.get("status") == "conflict"
            references_valid = (bool(references) or not needs_reference) and all(
                isinstance(ref, dict) and isinstance(ref.get("quote"), str) and bool(ref["quote"])
                and ref.get("id") in catalog and ref["quote"] in catalog[ref["id"]] for ref in references
            )
            checks.append({"id": criterion["id"], "requirement": criterion["requirement"],
                           "expected_score": criterion["score"], "actual_score": observed.get("score"),
                           "expected_status": criterion["status"], "actual_status": observed.get("status"),
                           "score_match": observed.get("score") == criterion["score"],
                           "status_match": observed.get("status") == criterion["status"],
                           "requirement_match": observed.get("requirement") == criterion["requirement"],
                           "positive_or_conflict": needs_reference, "references_valid": references_valid})
        weights = baseline["jobs"][target["job_id"]]["effective_weights"]
        dimension_scores = {name: round(sum(c.get("score", 0) for c in dim["criteria"]) / len(dim["criteria"]), 2)
                            if dim["criteria"] else 0 for name, dim in dimensions.items()}
        weights_valid = dimensions.keys() == weights.keys() and all(
            math.isclose(dimensions[name]["weight"], weight, abs_tol=1e-9) for name, weight in weights.items())
        arithmetic_valid = all(dim["score"] == dimension_scores[name] for name, dim in dimensions.items()) and row["total_score"] == round(
            sum(dimension_scores[name] * dim["weight"] for name, dim in dimensions.items()), 2)
        version = snapshot.get("input_version") or {}
        jd_text = baseline["jd_texts"][target["job_id"]]
        canonical_jd_hash = hashlib.sha256(jd_text.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")).hexdigest()
        context_valid = (row.get("evaluation_version") == baseline["scoring_rules_version"]
                         and (snapshot.get("job") or {}).get("raw_jd") == jd_text
                         and version.get("jd_hash") == canonical_jd_hash
                         and version.get("application_id") == row["application_id"]
                         and version.get("job_version_id") == row.get("job_version_id")
                         and bool(row.get("model")) and bool(row.get("prompt_version")) and row.get("is_stale") is False)
        checks_ok = all(all(c[k] for k in ("score_match", "status_match", "requirement_match", "references_valid")) for c in checks)
        valid = checks_ok and criterion_set_valid and weights_valid and arithmetic_valid and context_valid
        valid = valid and not row.get("evaluation_error") and row.get("facts_unchanged") is True and row.get("no_hiring_decision") is True
        pairs.append({"id": target["id"], "question_id": target["question_id"], "job_id": target["job_id"],
                      "expected_total": target["total_score"], "actual_total": row["total_score"],
                      "total_match": row["total_score"] == target["total_score"], "criteria": checks,
                      "criterion_set_match": criterion_set_valid, "weights_valid": weights_valid,
                      "arithmetic_valid": arithmetic_valid, "context_valid": context_valid,
                      "facts_unchanged": row.get("facts_unchanged"), "no_hiring_decision": row.get("no_hiring_decision"),
                      "automated_match": bool(valid and row["total_score"] == target["total_score"])})
    anchors = []
    for relation in baseline["comparisons"]:
        higher, lower = relation["relation"].split(" > ")
        job = relation["job_id"]
        higher_score, lower_score = actual[higher, job]["total_score"], actual[lower, job]["total_score"]
        anchors.append({**relation, "passed": higher_score is not None and lower_score is not None and higher_score > lower_score})
    ordering_changes = []
    direction = lambda value: (value > 0) - (value < 0)
    for left, right in combinations(pairs, 2):
        if left["job_id"] != right["job_id"]:
            continue
        if left["actual_total"] is None or right["actual_total"] is None:
            continue  # 失败请求没有分数，不能伪装成0分参与排序。
        before = direction(left["expected_total"] - right["expected_total"])
        after = direction(left["actual_total"] - right["actual_total"])
        if before != after:
            ordering_changes.append({"left": left["id"], "right": right["id"], "expected_relation": before,
                                     "actual_relation": after, "kind": "reversed" if before * after == -1 else "tie_changed"})
    criteria = [c for pair in pairs for c in pair["criteria"]]
    passed = all(pair["automated_match"] for pair in pairs) and all(item["passed"] for item in anchors)
    return {"baseline_version": baseline["version"], "human_signoff": True,
            "automated_check": "passed" if passed else "failed",
            "semantic_acceptance": "pending_independent_review" if passed else "not_passed",
            "summary": {"pairs": len(pairs), "total_matches": sum(p["total_match"] for p in pairs),
                        "failed_evaluations": sum(bool(row.get("evaluation_error")) for row in results),
                        "criterion_score_matches": sum(c["score_match"] for c in criteria),
                        "criterion_score_status_matches": sum(c["score_match"] and c["status_match"] for c in criteria),
                        "positive_or_conflict_criteria": sum(c["positive_or_conflict"] for c in criteria),
                        "invalid_reference_criteria": sum(not c["references_valid"] for c in criteria),
                        "automated_pair_matches": sum(p["automated_match"] for p in pairs)},
            "pairs": pairs, "required_rankings": anchors,
            "derived_ordering_changes": ordering_changes,
            "limits": "自动比较只查分数、状态、权重、引用存在性及数据隔离；引用是否支持语义另审。同岗衍生排序仅诊断，不新增门槛。"}
