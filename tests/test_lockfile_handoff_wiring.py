"""Authority journaling and scheduler admission for lockfile handoffs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_workbench.authority_service import AuthorityService, is_read_only_tool
from codex_workbench.config import WorkbenchConfig
from codex_workbench.mcp import TOOLS, WorkbenchMCPServer
from codex_workbench.model import NodeSpec, TaskContract
from codex_workbench.store import CommandConflictError, StateConflictError, WorkbenchStore


class LockfileHandoffWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = WorkbenchConfig(self.root / 'state')
        self.config.initialize()
        self.store = WorkbenchStore(self.config.database)
        self.store.initialize()

    def test_catalog_and_read_only_dispatch_do_not_create_journal_rows(self) -> None:
        self.assertEqual(sum(tool['name'] == 'workbench_handoff_lockfile' for tool in TOOLS), 1)
        server = WorkbenchMCPServer(self.config, self.store)
        service = AuthorityService(self.store, server._tool_result, 'authority-fixture')
        for op in ('preview', 'status'):
            args = {'op': op, 'task_id': 'fixture-task'}
            self.assertTrue(is_read_only_tool('workbench_handoff_lockfile', args))
            with patch('codex_workbench.mcp.lockfile_handoff', return_value={'ok': True}) as handler:
                response = service.dispatch({'tool': 'workbench_handoff_lockfile', 'arguments': args})
            self.assertEqual(response['state'], 'completed')
            handler.assert_called_once_with(self.config, self.store, args)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('select count(*) from authority_requests').fetchone()[0], 0)

    def test_mutation_journal_replays_receipt_without_reinvoking_handoff(self) -> None:
        calls = []
        def invoke(name, args):
            calls.append((name, args))
            return {'ok': True, 'state': 'ready'}
        service = AuthorityService(self.store, invoke, 'authority-fixture')
        envelope = {
            'request_id': 'handoff-once',
            'tool': 'workbench_handoff_lockfile',
            'arguments': {'op': 'apply', 'task_id': 'fixture-task', 'request_id': 'handoff-once'},
        }
        first = service.dispatch(envelope)
        second = service.dispatch(envelope)
        self.assertEqual(first['result'], second['result'])
        self.assertEqual(len(calls), 1)
        with self.assertRaises(CommandConflictError):
            service.dispatch({**envelope, 'arguments': {**envelope['arguments'], 'expected_fingerprint': 'changed'}})
        for op in ('apply', 'cancel', 'reconcile'):
            bad = {**envelope, 'arguments': {'op': op, 'task_id': 'fixture-task', 'request_id': 'another-id'}}
            with self.assertRaises(ValueError):
                service.dispatch(bad)

    def test_reconcile_has_separate_journal_identity(self) -> None:
        service = AuthorityService(self.store, lambda *_: {'ok': True}, 'authority-fixture')
        result = service.dispatch({
            'tool': 'workbench_handoff_lockfile', 'request_id': 'reconcile-once',
            'arguments': {'op': 'reconcile', 'task_id': 'fixture-task',
                          'request_id': 'original-handoff', 'operation_id': 'reconcile-once'},
        })
        self.assertEqual(result['state'], 'completed')

    def test_drain_keeps_status_readable_without_admitting_new_handoffs(self) -> None:
        service = AuthorityService(self.store, lambda *_: {'ok': True}, 'authority-fixture')
        service.begin_drain()
        status = service.dispatch({'tool': 'workbench_handoff_lockfile',
                                   'arguments': {'op': 'status', 'task_id': 'fixture-task'}})
        self.assertEqual(status['state'], 'completed')
        with self.assertRaises(StateConflictError):
            service.dispatch({'tool': 'workbench_handoff_lockfile', 'request_id': 'handoff-once',
                              'arguments': {'op': 'apply', 'task_id': 'fixture-task', 'request_id': 'handoff-once'}})

    def test_existing_scheduler_waits_for_handoff_owner_then_claims_normally(self) -> None:
        epoch = self.store.activate_coordinator('claim-fixture', 'machine-fixture')
        contract = TaskContract('fixture-task', str(self.root), 'fixture-base',
                                'fixture handoff admission', allowed_scope=('pnpm-lock.yaml',))
        self.store.create_task(contract, [
            NodeSpec('work', 'fixture-task', 'worker', 'fixture', 'fixture', 'fixture'),
            NodeSpec('verify', 'fixture-task', 'verifier', 'fixture', 'fixture', 'fixture',
                     depends_on=('work',), verifier=True),
        ], 'create-fixture')
        self.store.queue_task('fixture-task')
        with patch('codex_workbench.lockfile_handoff.active_lockfile_handoff', return_value={'state': 'reserved'}):
            self.assertIsNone(self.store.claim_ready_node('worker-fixture', epoch))
        with patch('codex_workbench.lockfile_handoff.active_lockfile_handoff', return_value=None):
            claimed = self.store.claim_ready_node('worker-fixture', epoch)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed['node_id'], 'work')
        self.assertEqual(claimed['attempt'], 1)

    def test_schema_fourteen_upgrade_preserves_tasks_and_fences_old_coordinators(self) -> None:
        contract = TaskContract('migration-fixture', str(self.root), 'fixture-base',
                                'fixture migration', allowed_scope=('pnpm-lock.yaml',))
        self.store.create_task(contract, [
            NodeSpec('work', contract.task_id, 'worker', 'fixture', 'fixture', 'fixture'),
            NodeSpec('verify', contract.task_id, 'verifier', 'fixture', 'fixture', 'fixture',
                     depends_on=('work',), verifier=True),
        ], 'migration-fixture-create')
        before = self.store.get_task(contract.task_id)
        with self.store.connection() as connection:
            connection.execute("UPDATE metadata SET value='14' WHERE key='schema_version'")
            connection.execute('DROP INDEX events_type_cursor_idx')
            events = connection.execute('SELECT * FROM events ORDER BY cursor').fetchall()
            events_before = [tuple(row) for row in events]
        self.store.initialize()
        self.assertEqual(self.store.health()['schema_version'], 15)
        self.assertEqual(self.store.get_task(contract.task_id), before)
        with self.store.connection() as connection:
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM events ORDER BY cursor')], events_before)
            self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='events_type_cursor_idx'").fetchone())
            dump = tuple(connection.iterdump())
        with patch('codex_workbench.store.SCHEMA_VERSION', 14):
            with self.assertRaisesRegex(RuntimeError, 'unsupported schema version 15; expected 14'):
                self.store.initialize()
        with self.store.connection() as connection:
            self.assertEqual(tuple(connection.iterdump()), dump)
