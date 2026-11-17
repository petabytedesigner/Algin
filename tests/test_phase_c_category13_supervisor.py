"""Category 13 offline supervisor: crash/Termux/network/lease fault invariants.

All subprocess and heartbeat responses are synthetic; zero OANDA requests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import io
import json
import os
import unittest

import config
from operations.forward_journal import report_runs, read_run
from operations.forward_supervisor import (
    EXPECTED_ACCOUNT, ExclusiveSupervisor, ForwardBlocked,
    ForwardSupervisor, account_environment, main, verify_readonly,
)
from storage.heartbeat import HeartbeatError
from utils.time_tools import iso_utc


NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
TOKEN = "synthetic-token-not-a-real-token-0123456789"


class Clock:
    def __init__(self):
        self.value = NOW

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeChild:
    def __init__(self, *, exit_code=None):
        self.pid = 21432
        self.exit_code = exit_code
        self.terminations = 0

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminations += 1
        self.exit_code = -15

    def kill(self):
        self.terminations += 1
        self.exit_code = -9

    def wait(self, timeout=None):
        return self.exit_code


class FakeHeartbeat:
    def __init__(self, clock, child):
        self.clock = clock
        self.child = child
        self.state = "CYCLE_COMPLETE"
        self.reason = "NONE"
        self.pid_override = None
        self.stale_seconds = 0
        self.fixed_stamp = None
        self.unavailable = False
        self.read_calls = 0

    def read(self):
        self.read_calls += 1
        if self.unavailable:
            raise HeartbeatError("Synthetic missing/corrupt")
        stamp = (self.fixed_stamp if self.fixed_stamp is not None
                 else self.clock.now() - timedelta(seconds=self.stale_seconds))
        return {
            "version": 1, "timestamp": iso_utc(stamp),
            "pid": self.pid_override or self.child.pid,
            "state": self.state, "reason_code": self.reason,
            "cycle_id": "synthetic",
        }

    def inspect(self, *, now, max_age_seconds):
        row=self.read()
        age=(now - datetime.fromisoformat(
            row["timestamp"].replace("Z","+00:00")
        )).total_seconds()
        health=("STALE" if age>max_age_seconds else
                "BLOCKED" if row["state"]=="BLOCKED" else row["state"])
        return {"health":health, "state":row["state"],
                "reason_code":row["reason_code"], "age_seconds":int(age)}


class ForwardSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/"main.py").write_text("# synthetic app, never executed\n")
        self.clock=Clock()
        self.child=FakeChild()
        self.hb=FakeHeartbeat(self.clock,self.child)
        self.spawned=[]
        self.env={"OANDA_ACCOUNT_ID":EXPECTED_ACCOUNT,
                  "OANDA_API_TOKEN":TOKEN}

    def launch(self, args, *, cwd, env, stdin, stdout, stderr):
        self.spawned.append({
            "args":args, "cwd":cwd, "env":dict(env),
            "stdin":stdin, "stderr":stderr,
        })
        return self.child

    def watch(self, *, samples=2, interval=30, grace=120,
              max_age=180):
        return ForwardSupervisor(
            root=self.root, interval=interval,
            startup_grace=grace, max_age=max_age,
            samples=samples, clock=self.clock.now,
            sleeper=self.clock.sleep, launcher=self.launch,
            heartbeat=self.hb,
        ).run(env=self.env)

    def history(self):
        return report_runs(self.root/".local"/"forward-runs")

    def test_normal_observe_only_window_never_places_orders(self):
        self.assertEqual(self.watch(samples=3),0)
        self.assertEqual(len(self.spawned),1)
        self.assertEqual(self.spawned[0]["args"][-1],"main.py")
        self.assertNotIn("--trade",self.spawned[0]["args"])
        self.assertEqual(self.spawned[0]["cwd"],str(self.root))
        self.assertEqual(self.child.terminations,1)
        self.assertEqual(self.history()["healthy_samples"],3)
        self.assertEqual(self.history()["completed_runs"],1)
        self.assertFalse((self.root/".local"/"forward-supervisor.lock").exists())
        logs=list((self.root/".local"/"forward-logs").glob("*.log"))
        self.assertEqual(len(logs),1)

    def test_session_closed_remains_gated_not_tradeable(self):
        self.hb.state="BLOCKED"
        self.hb.reason="SESSION_BLOCK"
        self.assertEqual(self.watch(samples=2),0)
        self.assertEqual(self.history()["gated_samples"],2)
        self.assertEqual(self.history()["healthy_samples"],0)
        self.assertEqual(self.child.terminations,1)

    def test_http_401_or_broker_unknown_blocks_without_restart(self):
        self.hb.state="BLOCKED"
        self.hb.reason="BROKER_UNAVAILABLE"
        self.assertEqual(self.watch(samples=4),3)
        self.assertEqual(len(self.spawned),1)
        self.assertEqual(self.child.terminations,1)
        self.assertEqual(self.history()["recent_block_reason"],
                         "BOT_BROKER_UNAVAILABLE")

    def test_unreconciled_exposure_is_fatal_not_healthy(self):
        self.hb.state="BLOCKED"
        self.hb.reason="UNRECONCILED_EXPOSURE"
        self.assertEqual(self.watch(),3)
        self.assertEqual(self.history()["blocked_runs"],1)

    def test_audit_failure_never_qualified_as_success(self):
        self.hb.state="BLOCKED"
        self.hb.reason="AUDIT_FAILURE"
        self.assertEqual(self.watch(),3)
        self.assertEqual(self.history()["healthy_samples"],0)

    def test_stale_heartbeat_causes_no_restart(self):
        # A heartbeat created by THIS child at startup, followed by a
        # deliberate 240-second lapse, is stale. A pre-launch heartbeat
        # is separately classified as OLD, not as same-process liveness.
        self.hb.fixed_stamp = NOW
        self.clock.sleep = lambda seconds: setattr(
            self.clock, "value",
            self.clock.value + timedelta(seconds=240),
        )
        self.assertEqual(self.watch(max_age=180),3)
        self.assertEqual(self.history()["recent_block_reason"],
                         "STALE_HEARTBEAT")
        self.assertEqual(len(self.spawned),1)

    def test_one_atomic_heartbeat_read_per_sample(self):
        self.assertEqual(self.watch(samples=3), 0)
        # The PID and state must come from the same validated read,
        # not two separate heartbeat files across an atomic replace.
        self.assertEqual(self.hb.read_calls, 3)

    def test_future_heartbeat_blocks_without_applying_foreign_clock(self):
        self.hb.fixed_stamp = NOW + timedelta(minutes=5)
        self.assertEqual(self.watch(), 3)
        self.assertEqual(self.history()["recent_block_reason"],
                         "FUTURE_HEARTBEAT")

    def test_foreign_pid_heartbeat_is_not_child_liveness(self):
        self.hb.pid_override=54321
        self.assertEqual(self.watch(),3)
        self.assertEqual(self.history()["recent_block_reason"],
                         "FOREIGN_HEARTBEAT")

    def test_old_heartbeat_does_not_count_as_valid_sample(self):
        self.hb.stale_seconds=7200
        self.assertEqual(self.watch(samples=3,grace=45),3)
        self.assertEqual(self.history()["recent_block_reason"],
                         "STARTUP_NO_HEARTBEAT")
        self.assertEqual(self.history()["healthy_samples"],0)

    def test_missing_or_corrupt_hb_causes_startup_fail_closed(self):
        self.hb.unavailable=True
        self.assertEqual(self.watch(samples=3,grace=45),3)
        self.assertEqual(self.history()["completed_runs"],0)
        self.assertEqual(self.child.terminations,1)

    def test_bot_exit_does_not_auto_spawn_second_process(self):
        self.child.exit_code=1
        self.assertEqual(self.watch(samples=2),3)
        self.assertEqual(len(self.spawned),1)
        self.assertEqual(self.history()["recent_block_reason"],
                         "BOT_PROCESS_EXITED")

    def test_exclusive_lock_rejects_concurrent_supervisor(self):
        with ExclusiveSupervisor(self.root):
            with self.assertRaises(ForwardBlocked):
                with ExclusiveSupervisor(self.root):
                    pass
        self.assertFalse((self.root/".local"/"forward-supervisor.lock").exists())

    def test_orphan_lock_requires_explicit_operator_review(self):
        lock=self.root/".local"/"forward-supervisor.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("pid=9999 orphan")
        with self.assertRaises(ForwardBlocked):
            self.watch()
        self.assertEqual(lock.read_text(),"pid=9999 orphan")
        self.assertEqual(self.spawned,[])

    def test_tampered_lock_is_not_unlinked_by_owner(self):
        lock=self.root/".local"/"forward-supervisor.lock"
        with self.assertRaises(ForwardBlocked):
            with ExclusiveSupervisor(self.root):
                lock.write_text("another-writer")
        self.assertTrue(lock.exists())

    def test_spawn_error_is_durable_block_not_fabricated_uptime(self):
        def reject(*a,**k):
            raise OSError("offline synthetic process spawn fail")
        supervisor=ForwardSupervisor(
            root=self.root,interval=30,samples=1,
            clock=self.clock.now,sleeper=self.clock.sleep,
            launcher=reject,heartbeat=self.hb)
        with self.assertRaises(OSError):
            supervisor.run(env=self.env)
        self.assertEqual(self.history()["blocked_runs"],1)
        self.assertEqual(self.history()["samples"],0)

    def test_monitor_zero_runtime_credentials_never_emitted_in_evidence(self):
        self.assertEqual(self.watch(samples=1),0)
        files=list((self.root/".local"/"forward-runs").glob("*.jsonl"))
        self.assertEqual(len(files),1)
        content=files[0].read_text()
        self.assertNotIn(TOKEN,content)
        self.assertNotIn(EXPECTED_ACCOUNT,content)
        self.assertTrue(all("sha256" in line for line in content.splitlines()))

    def test_policy_rejects_released_production_write_gates(self):
        with patch.object(config,"ORDER_WRITES_ENABLED",True):
            with self.assertRaises(ForwardBlocked):
                self.watch()
        with patch("operations.forward_supervisor._PRACTICE_WRITES_RELEASED",
                   True):
            with self.assertRaises(ForwardBlocked):
                self.watch()
        self.assertEqual(self.spawned,[])

    def test_wrong_account_and_missing_credentials_block_before_launch(self):
        self.env["OANDA_ACCOUNT_ID"]="other"
        with self.assertRaises(ForwardBlocked):
            self.watch()
        self.env["OANDA_ACCOUNT_ID"]=EXPECTED_ACCOUNT
        self.env["OANDA_API_TOKEN"]=""
        with self.assertRaises(ForwardBlocked):
            self.watch()
        self.assertEqual(self.spawned,[])

    def test_outside_repo_does_not_launch(self):
        (self.root/"main.py").unlink()
        with self.assertRaises(ForwardBlocked):
            self.watch()
        self.assertEqual(self.spawned,[])


class CredentialBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def demo(self, text):
        (self.root/".env.example").write_text(text)

    def test_existing_demo_credentials_loaded_without_shell_execution(self):
        self.demo("OANDA_API_TOKEN="+TOKEN+"\nOANDA_ACCOUNT_ID="+EXPECTED_ACCOUNT+"\n")
        result=account_environment(self.root,environ={})
        self.assertEqual(result["OANDA_ACCOUNT_ID"],EXPECTED_ACCOUNT)
        self.assertEqual(result["OANDA_API_TOKEN"],TOKEN)

    def test_explicit_environment_credentials_override_file(self):
        self.demo("untrusted shell code not parsed here")
        result=account_environment(self.root,environ={
            "OANDA_API_TOKEN":TOKEN,
            "OANDA_ACCOUNT_ID":EXPECTED_ACCOUNT,
        })
        self.assertEqual(result["OANDA_API_TOKEN"],TOKEN)

    def test_partial_env_is_rejected_not_silently_mixed_with_file(self):
        self.demo("OANDA_API_TOKEN="+TOKEN+"\nOANDA_ACCOUNT_ID="+EXPECTED_ACCOUNT+"\n")
        with self.assertRaises(ForwardBlocked):
            account_environment(self.root,environ={"OANDA_API_TOKEN":TOKEN})

    def test_malformed_or_extra_key_or_duplicate_is_blocked(self):
        for data in (
            "OANDA_API_TOKEN="+TOKEN+"\nOANDA_ACCOUNT_ID="+EXPECTED_ACCOUNT+"\nBAD=1\n",
            "OANDA_API_TOKEN="+TOKEN+"\nOANDA_API_TOKEN="+TOKEN+"\n",
            "OANDA_API_TOKEN="+TOKEN+"; rm -rf /\nOANDA_ACCOUNT_ID="+EXPECTED_ACCOUNT+"\n",
        ):
            with self.subTest(data=data[:18]):
                self.demo(data)
                with self.assertRaises(ForwardBlocked):
                    account_environment(self.root,environ={})

    def test_wrong_account_and_symlinked_env_are_rejected(self):
        self.demo("OANDA_API_TOKEN="+TOKEN+"\nOANDA_ACCOUNT_ID=wrong\n")
        with self.assertRaises(ForwardBlocked):
            account_environment(self.root,environ={})
        path=self.root/".env.example"
        path.unlink()
        path.symlink_to(self.root/"somewhere")
        with self.assertRaises(ForwardBlocked):
            account_environment(self.root,environ={})

    def test_offline_cli_check_never_loads_token(self):
        with patch("operations.forward_supervisor.account_environment",
                   side_effect=AssertionError("must not open env")),patch(
                       "sys.stdout",new_callable=io.StringIO) as output:
            self.assertEqual(main(["--check"]),0)
            self.assertIn("OFFLINE_POLICY_PASS",output.getvalue())


if __name__=="__main__":
    unittest.main()
