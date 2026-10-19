"""Collector regressions without starting iTerm2 or real tracing commands."""
import asyncio
import importlib.util
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

sys.modules.setdefault('iterm2', types.SimpleNamespace(run_forever=lambda main: None))
spec = importlib.util.spec_from_file_location('sysmon', Path(__file__).with_name('sysmon.py'))
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        monitor.CONN_SNAPSHOT.update(at=None, out='')
        monitor.CONN_PREV.clear()
        monitor.ACTIVITY_CLIENTS.clear()
        monitor.NETWORK_ACTIVE.clear()
        monitor.FILEOPS_ACTIVE.clear()

    def test_concurrent_connection_requests_share_one_command(self):
        barrier = threading.Barrier(8)
        def collect(args):
            time.sleep(0.05)
            return 'snapshot'
        def request():
            barrier.wait()
            return monitor._connection_snapshot()
        with patch.object(monitor, '_sh', side_effect=collect) as command:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: request(), range(8)))
        self.assertEqual(command.call_count, 1)
        self.assertEqual(len(set(results)), 1)

    def test_failed_snapshot_is_cached_and_retried_after_ttl(self):
        with patch.object(monitor, '_sh', return_value='') as command:
            monitor._connection_snapshot()
            monitor._connection_snapshot()
            self.assertEqual(command.call_count, 1)
            monitor.CONN_SNAPSHOT['at'] -= 6
            monitor._connection_snapshot()
            self.assertEqual(command.call_count, 2)

    def test_cached_reads_do_not_advance_connection_rate_baseline(self):
        output = ',state,bytes_in,bytes_out,\nexample.42,,0,0,\ntcp4 127.0.0.1:5000<->127.0.0.1:443,Established,120,60,\n'
        monitor.CONN_PREV[(42, 'tcp4 127.0.0.1:5000<->127.0.0.1:443')] = (20, 10, 10)
        with patch.object(monitor, '_connection_snapshot', return_value=(output, 15)), patch.object(monitor, '_rdns', return_value=''):
            first = monitor.read_conns('example')
            second = monitor.read_conns('example')
        self.assertEqual(first['conns'][0]['rate'], [20, 10])
        self.assertIsNone(second['conns'][0]['rate'])
        self.assertEqual(monitor.CONN_PREV[(42, 'tcp4 127.0.0.1:5000<->127.0.0.1:443')][2], 15)

    def test_collectors_wait_without_launching_commands(self):
        for event, collector in ((monitor.FILEOPS_ACTIVE, monitor.fileops_monitor),
                                 (monitor.NETWORK_ACTIVE, monitor.nettop_stream)):
            with patch.object(event, 'wait', side_effect=InterruptedError), patch.object(monitor.subprocess, 'Popen') as popen, patch.object(monitor, 'start_fsevents') as events:
                with self.assertRaises(InterruptedError):
                    collector()
                popen.assert_not_called()
                events.assert_not_called()

    def test_activity_is_opt_in_and_pause_terminates_collectors(self):
        network_proc, file_proc = Mock(), Mock()
        network_proc.poll.return_value = file_proc.poll.return_value = None
        api = Mock()
        with patch.object(monitor, 'NETTOP_PROC', network_proc), patch.object(monitor, 'FS_USAGE_PROC', file_proc), patch.object(monitor, 'FSEVENTS_API', api), patch.object(monitor, 'FSEVENTS_STREAM', 123), patch.object(monitor, 'FSEVENTS_RUNNING', True):
            monitor.record_activity('viewer', True, True)
            self.assertTrue(monitor.NETWORK_ACTIVE.is_set())
            self.assertTrue(monitor.FILEOPS_ACTIVE.is_set())
            monitor.record_activity('viewer', False, False)
            self.assertFalse(monitor.NETWORK_ACTIVE.is_set())
            self.assertFalse(monitor.FILEOPS_ACTIVE.is_set())
            network_proc.terminate.assert_called_once()
            file_proc.terminate.assert_called_once()
            api.FSEventStreamStop.assert_called_once_with(123)

    def test_viewers_do_not_pause_each_other_and_expire(self):
        monitor.record_activity('one', True, True)
        monitor.record_activity('two', False, False)
        self.assertTrue(monitor.NETWORK_ACTIVE.is_set())
        self.assertTrue(monitor.FILEOPS_ACTIVE.is_set())
        monitor.ACTIVITY_CLIENTS['one'] = (time.monotonic() - 31, True, True)
        with monitor.ACTIVITY_LOCK:
            monitor._apply_activity_locked()
        self.assertFalse(monitor.NETWORK_ACTIVE.is_set())
        self.assertFalse(monitor.FILEOPS_ACTIVE.is_set())
        self.assertNotIn('one', monitor.ACTIVITY_CLIENTS)

    def test_fsevents_starts_only_on_demand_and_can_restart(self):
        api = Mock()
        with patch.object(monitor, 'FSEVENTS_STREAM', 123), patch.object(monitor, 'FSEVENTS_API', api), patch.object(monitor, 'FSEVENTS_RUNNING', False):
            monitor._enable_fsevents('hint')
            api.FSEventStreamStart.assert_not_called()
            monitor.FILEOPS_ACTIVE.set()
            monitor._enable_fsevents('hint')
            monitor._enable_fsevents('hint')
            api.FSEventStreamStart.assert_called_once_with(123)
            self.assertEqual(monitor.STATE['fileops_source'], 'fsevents')

    def test_second_instance_cannot_launch_collectors(self):
        with patch.object(monitor, 'MonitorServer', side_effect=OSError('port in use')), patch.object(monitor.threading, 'Thread') as worker:
            with self.assertRaises(OSError):
                asyncio.run(monitor.main(None))
        worker.assert_not_called()

    def test_hardware_constant_only_collected_once(self):
        monitor._hardware_sysctl.cache_clear()
        with patch.object(monitor, '_sysctl', return_value='1234') as command:
            self.assertEqual(monitor._hardware_sysctl('hw.memsize'), '1234')
            self.assertEqual(monitor._hardware_sysctl('hw.memsize'), '1234')
        command.assert_called_once()


if __name__ == '__main__':
    unittest.main()
