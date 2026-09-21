from experiments.harness_ablation import run_benchmark


def test_harness_mechanism_ablation_is_reproducible():
    result = run_benchmark()
    recovery = result["recovery_ablation"]
    assert recovery["same_failed_prefix"]
    assert recovery["resume_completed"] and recovery["restart_completed"]
    assert recovery["additional_stage_calls"] == {"checkpoint_resume": 1, "stateless_restart": 2}
    assert recovery["completed_stage_replayed"] == {"checkpoint_resume": 0, "stateless_restart": 1}
    assert recovery["work_reduction_fraction"] == 0.5

    evidence = result["evidence_gate_ablation"]
    assert evidence["clean_package_accepted"]
    assert evidence["naive_completed_gate_accepts_tampered_package"]
    assert not evidence["strict_gate_accepts_tampered_package"]
    assert evidence["tamper_checks"] == ["artifact:code/main.py"]
    assert evidence["naive_completed_gate_accepts_weak_review"]
    assert not evidence["strict_gate_accepts_weak_review"]
    assert evidence["review_checks"] == ["review:recommendation", "review:no-major-weaknesses"]
