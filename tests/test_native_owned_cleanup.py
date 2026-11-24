"""Owned native process-group cleanup; no kernel isolation or provider claim."""
import os
import socket
import time
import unittest

from floati.worker_bootstrap_protocol import BootstrapChannel
from floati.worker_exec import SpawnedWorkerProcess
from floati.workers import WorkerRunner
from tests.readiness import Readiness


class OwnedGroupCleanupTests(unittest.TestCase):
    def test_shared_finish_does_not_reap_owner_before_closed_pipe_descendant_cleanup(self):
        """Owned process-group proof only; this does not exercise kernel isolation."""
        parent_socket, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        leader = os.fork()
        if leader == 0:
            try:
                parent_socket.close()
                os.setsid()
                descendant = os.fork()
                if descendant == 0:
                    child_socket.close()
                    # Safety fallback outlives both readiness handshakes.
                    time.sleep(120)
                    os._exit(0)
                channel = BootstrapChannel(child_socket.detach())
                channel.send(("fixture_group_ready", descendant))
                readiness = Readiness("owned group parent confirmation")
                try:
                    confirmed = channel.recv(timeout=readiness.remaining())
                except Exception as error:
                    os.write(2, (str(readiness.failure()) + "\n").encode())
                    raise error
                if confirmed != ("fixture_group_confirmed", None):
                    os._exit(1)
                channel.close()
            finally:
                os._exit(0)
        child_socket.close()
        process = SpawnedWorkerProcess(leader)
        channel = BootstrapChannel(parent_socket.detach())
        self.addCleanup(channel.close)
        self.addCleanup(process.join, 5)
        def cleanup_owned_group():
            # A readiness failure must also bind and reap this owned fixture;
            # the normal path confirms the group immediately below.
            if process.confirm_process_group():
                process.shutdown_process_group()
            else:
                process.terminate()
        self.addCleanup(cleanup_owned_group)
        readiness = Readiness("owned group descendant ready frame")
        try:
            verb, descendant = channel.recv(timeout=readiness.remaining())
        except Exception as error:
            raise readiness.failure() from error
        self.assertEqual(verb, "fixture_group_ready")
        self.assertTrue(process.confirm_process_group())
        channel.send(("fixture_group_confirmed", None))
        WorkerRunner._finish_effect_process(
            channel, process, time.monotonic() + 3,
            before_reap=process.shutdown_process_group)
        process.shutdown_process_group()
        process.join(1)
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant, 0)
