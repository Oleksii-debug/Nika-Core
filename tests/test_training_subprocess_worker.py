from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nika_core.training_adapters import SubprocessTrainingWorker, TrainingSubprocessError
from nika_core.training_runtime import ArtifactIdentity, TrainingJobSpec


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "trainer.py"
    path.write_text(body, encoding="utf-8")
    return path


def _spec(*, max_steps: int = 3) -> TrainingJobSpec:
    return TrainingJobSpec(
        job_id="job-1",
        task_id="task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity("models/base", "a" * 64),
        candidate_artifact_ref="models/candidate/job-1",
        max_steps=max_steps,
    )


def test_real_subprocess_step_resume_and_completion(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
step_index = request["step_index"]
response = {
    "candidate_sha256": "b" * 64 if step_index == 1 else None,
    "completed": step_index == 1,
    "protocol_version": 1,
    "resume_state": {"next_epoch": step_index + 1},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    first = worker.step(spec=_spec(), step_index=0, resume_state={})
    assert first.completed is False
    assert first.candidate_sha256 is None

    second = worker.step(spec=_spec(), step_index=1, resume_state=first.resume_state)
    assert second.completed is True
    assert second.candidate_sha256 == "b" * 64


def test_step_identity_is_stable_for_replay(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {"observed_step_id": request["step_id"]},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    first = worker.step(spec=_spec(), step_index=0, resume_state={})
    replay = worker.step(spec=_spec(), step_index=0, resume_state={})

    first_envelope = first.resume_state["_nika_subprocess"]
    replay_envelope = replay.resume_state["_nika_subprocess"]
    assert isinstance(first_envelope, dict)
    assert isinstance(replay_envelope, dict)
    assert first_envelope["last_step_id"] == replay_envelope["last_step_id"]


def test_parent_environment_is_not_inherited_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIKA_TRAINING_SECRET", "must-not-leak")
    trainer = _script(
        tmp_path,
        """
import json
import os
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {"secret_seen": os.getenv("NIKA_TRAINING_SECRET") is not None},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    result = worker.step(spec=_spec(), step_index=0, resume_state={})

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["secret_seen"] is False


def test_explicit_environment_is_the_only_environment_exposed(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import os
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {"allowed": os.getenv("NIKA_ALLOWED")},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker(
        (sys.executable, str(trainer)), environment={"NIKA_ALLOWED": "yes"}
    )

    result = worker.step(spec=_spec(), step_index=0, resume_state={})

    envelope = result.resume_state["_nika_subprocess"]
    assert isinstance(envelope, dict)
    trainer_state = envelope["trainer_state"]
    assert isinstance(trainer_state, dict)
    assert trainer_state["allowed"] == "yes"


def test_nonzero_exit_does_not_expose_stderr(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys
sys.stderr.write("TOP-SECRET-TRAINING-DATA")
raise SystemExit(9)
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError) as exc_info:
        worker.step(spec=_spec(), step_index=0, resume_state={})

    assert "TOP-SECRET-TRAINING-DATA" not in str(exc_info.value)
    assert "9" in str(exc_info.value)


def test_timeout_is_bounded_and_minimized(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import time
time.sleep(10)
""".strip(),
    )
    worker = SubprocessTrainingWorker(
        (sys.executable, str(trainer)), timeout_seconds=0.1
    )

    with pytest.raises(TrainingSubprocessError, match="timed out"):
        worker.step(spec=_spec(), step_index=0, resume_state={})


def test_oversized_response_is_rejected_during_execution(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import sys

sys.stdin.buffer.read()
chunk = b"x" * 65536
while True:
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
""".strip(),
    )
    worker = SubprocessTrainingWorker(
        (sys.executable, str(trainer)),
        max_response_bytes=1024,
        timeout_seconds=5,
    )

    with pytest.raises(TrainingSubprocessError, match="response exceeds"):
        worker.step(spec=_spec(), step_index=0, resume_state={})


def test_wrong_step_identity_fails_closed(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {},
    "step_id": "0" * 64,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="wrong step identity"):
        worker.step(spec=_spec(), step_index=0, resume_state={})


def test_unknown_response_field_fails_closed(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {},
    "step_id": request["step_id"],
    "unexpected": True,
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="unexpected fields"):
        worker.step(spec=_spec(), step_index=0, resume_state={})


def test_invalid_candidate_digest_is_minimized(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": "not-a-digest",
    "completed": True,
    "protocol_version": 1,
    "resume_state": {},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="invalid result evidence"):
        worker.step(spec=_spec(), step_index=0, resume_state={})


def test_invalid_resume_state_is_rejected_before_process_effect(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    trainer = _script(
        tmp_path,
        f"""
from pathlib import Path
Path({str(marker)!r}).write_text("started", encoding="utf-8")
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))

    with pytest.raises(TrainingSubprocessError, match="non-JSON"):
        worker.step(
            spec=_spec(),
            step_index=1,
            resume_state={"bad": object()},
        )

    assert not marker.exists()


def test_resume_state_cannot_cross_jobs(tmp_path: Path) -> None:
    trainer = _script(
        tmp_path,
        """
import json
import sys

request = json.loads(sys.stdin.buffer.read())
response = {
    "candidate_sha256": None,
    "completed": False,
    "protocol_version": 1,
    "resume_state": {"position": 1},
    "step_id": request["step_id"],
}
sys.stdout.write(json.dumps(response))
""".strip(),
    )
    worker = SubprocessTrainingWorker((sys.executable, str(trainer)))
    first = worker.step(spec=_spec(), step_index=0, resume_state={})
    changed = TrainingJobSpec(
        job_id="job-2",
        task_id="task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=ArtifactIdentity("models/base", "a" * 64),
        candidate_artifact_ref="models/candidate/job-2",
        max_steps=3,
    )

    with pytest.raises(TrainingSubprocessError, match="does not match the current job"):
        worker.step(spec=changed, step_index=1, resume_state=first.resume_state)


def test_command_must_not_be_a_shell_string() -> None:
    with pytest.raises(TypeError, match="not a shell string"):
        SubprocessTrainingWorker("python trainer.py")


def test_training_executable_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute path"):
        SubprocessTrainingWorker(("python", "trainer.py"))


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1.0, 0.0, True])
def test_invalid_timeout_is_rejected(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SubprocessTrainingWorker((os.path.abspath(sys.executable),), timeout_seconds=timeout)  # type: ignore[arg-type]


def test_huge_integer_timeout_is_rejected_without_overflow() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SubprocessTrainingWorker(
            (os.path.abspath(sys.executable),),
            timeout_seconds=10**10000,  # type: ignore[arg-type]
        )
