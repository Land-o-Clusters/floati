"""Root-bound role readers honor validated custom copy without artifact discovery."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.admin_registry import RegistryAdminBackend
from floati.node_wizard import NodeAddPlan
from floati.role_assignment import RoleAssignmentPlan
from floati.context import ContextTurnoverProjection, render_context_projection
from floati.doctor import _role_bindings, _role_cadences, _role_ack_slas
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.lane_scaling import LaneScalingService, RoleProfile
from floati.role_templates import load_role_template, load_shipped_role_templates
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_node_projections import MutableLedgerSource


class RoleLibraryConsumerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temp.name) / 'fleet', create=True)
        self.repo = Path(__file__).resolve().parents[1]
        templates = load_shipped_role_templates(self.repo / 'roles/shipped')
        payload = dict(templates['builder'].record, role='custom-builder', cadence='on-demand', ack_sla_minutes=17)
        target = self.root.path / 'roles/custom/custom-builder.json'
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(payload))
        self.template = load_role_template(target)
        self.source = MutableLedgerSource(self.root, templates)
        self.row = dict(self.source.roles['builder-a'], template_role='custom-builder',
                        template_sha256=self.template.digest, template_version=self.template.template_version)
        self.source.roles['builder-a'] = self.row
        backend = RegistryAdminBackend(self.root)
        backend.commit_add(NodeAddPlan(node_id='builder-a', harness='Codex', lifetime='permanent',
            lease_minutes=None, workspace=str(self.root.path / 'nodes/builder-a'),
            records=({'schema_version': 0, 'id': 'registry-018f7e9b3c137abc8def0123456789ab',
                      'tenant_id': self.root.tenant_id, 'timestamp': '2026-08-27T23:15:00.000Z',
                      'kind': 'registry_entry', 'node_id': 'builder-a', 'role': 'Codex', 'state': 'active'},),
            boot_command=None, teardown_command=None))
        backend.commit_role(RoleAssignmentPlan(node_id='builder-a', template_role='custom-builder', record=self.row))

    def test_doctor_root_bound_custom_cadence_and_ack_sla_remain_digest_checked(self):
        args = (self.repo, ['builder-a'], [self.row])
        self.assertEqual({'builder-a': 'on-demand'}, _role_cadences(*args, root=self.root))
        self.assertEqual({'builder-a': 17}, _role_ack_slas(*args, root=self.root))
        with self.assertRaises(IntegrityFailure):
            _role_bindings(self.repo, ['builder-a'], [dict(self.row, template_sha256='0' * 64)], root=self.root)
        with self.assertRaises(IntegrityFailure):
            _role_bindings(*args)

    def test_doctor_root_binding_preserves_explicit_shipped_source(self):
        source = self.root.path.parent / 'source'
        shipped = source / 'roles/shipped'
        shipped.mkdir(parents=True)
        for path in (self.repo / 'roles/shipped').glob('*.json'):
            (shipped / path.name).write_bytes(path.read_bytes())
        builder = shipped / 'builder.json'
        payload = json.loads(builder.read_text())
        payload['cadence'] = 'on-demand'
        builder.write_text(json.dumps(payload))
        template = load_role_template(builder)
        row = dict(self.row, template_role='builder', template_sha256=template.digest,
                   template_version=template.template_version)
        self.assertEqual({'builder-a': 'on-demand'},
                         _role_cadences(source, ['builder-a'], [row], root=self.root))
        with self.assertRaises(IntegrityFailure):
            _role_bindings(self.repo, ['builder-a'], [row], root=self.root)

    def test_context_project_and_root_bound_render_accept_custom_provenance(self):
        projection = ContextTurnoverProjection(self.root, 'builder-a', source=self.source)
        artifact = projection.project()
        self.assertEqual(self.template.digest, artifact['role_provenance']['template_sha256'])
        self.assertIn('custom-builder', projection.render())
        self.assertIn('custom-builder', render_context_projection(artifact, templates={'custom-builder': self.template}))

    def test_standalone_context_renderer_never_discovers_artifact_root(self):
        projection = ContextTurnoverProjection(self.root, 'builder-a', source=self.source)
        artifact = projection.project()
        with mock.patch('floati.role_library.RoleTemplateLibrary') as library:
            with self.assertRaises(ProtocolRefusal):
                render_context_projection(artifact)
            library.assert_not_called()

    def test_scaler_resolves_custom_profile_template_from_its_validated_root(self):
        profile = RoleProfile('custom', 'custom-builder', '/var/tmp/work-{instance}',
                              'Codex', 'permanent', None, {}, 'DRAFT role copy')
        service = LaneScalingService(self.root, {'custom': profile})
        self.assertEqual(self.template.digest, service.templates['custom-builder'].digest)


if __name__ == '__main__':
    unittest.main()
