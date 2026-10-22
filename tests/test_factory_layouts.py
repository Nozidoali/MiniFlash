import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from main import compile
import miniflash as flash
from miniflash.physical import MODES, check, resolve_mode

ROOT = Path(__file__).resolve().parents[1]


class FactoryLayouts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, body='t q[0];t q[1];t q[2];t q[3];', n=4, **kwargs):
        path = self.root / 'input.qasm'
        path.write_text(f'OPENQASM 2.0; include "qelib1.inc"; qreg q[{n}];\n' + body)
        return compile(path, cache_dir=ROOT / '.miniflash-cache', **kwargs)

    def test_default_and_explicit_override(self):
        for f in (1, 2, 3, 6):
            expected = 'in_place' if f <= 2 else 'backside_horizontal'
            actual = self.build(factories=f)
            explicit = self.build(factories=f, factory_mode=expected)
            self.assertEqual(actual.factory_mode, expected)
            self.assertEqual(actual.to_dict(), explicit.to_dict())
            for mode in MODES:
                p = self.build(factories=f, factory_mode=mode)
                self.assertEqual(p.factory_mode, mode)
                self.assertEqual(p.stats()['enabled_factory_count'], min(2,f) if mode=='in_place' else f)
                self.assertEqual(p.stats()['reserved_factory_count'], f)
                self.assertEqual(p.stats()['backside_injection_count'], 0 if mode=='in_place' else 4)
                self.assertEqual(p.stats()['in_place_injection_count'], 4 if mode=='in_place' else 0)

    def test_horizontal_preserves_compute_and_has_four_empty_rows(self):
        args = dict(body='h q[0];cx q[0],q[1];t q[0];t q[1];t q[2];t q[3];', factories=6)
        vertical = self.build(factory_mode='backside_vertical', **args).physical_layout
        horizontal = self.build(factory_mode='backside_horizontal', **args).physical_layout
        for key in ('layers', 'initial', 'parking', 'scratch'):
            self.assertEqual(horizontal[key], vertical[key])
        bottom = horizontal['horizontal_bank']['data_bottom']
        self.assertEqual({s['rect'][1] for s in horizontal['sites']}, {bottom+4})
        self.assertEqual(len({s['rect'][0] for s in horizontal['sites']}), 6)
        self.assertEqual(len({s['rect'][0] for s in vertical['sites']}), 1)

    def test_serialized_replay_rendering_and_accounting(self):
        for mode in MODES:
            p = self.build('h q[0];cx q[0],q[1];t q[0];tdg q[1];h q[1];t q[1];',
                           factories=6, factory_mode=mode)
            rebuilt = flash.Program.from_dict(json.loads(json.dumps(p.to_dict())))
            self.assertEqual(check(rebuilt), [])
            self.assertEqual(p.stats(), rebuilt.stats())
            s = rebuilt.stats()
            self.assertEqual(s['patch_cell_slices'], s['allocated_area_patches']*s['runtime_steps'])
            self.assertEqual(s['volume'], 8*s['patch_cell_slices'])
            self.assertAlmostEqual(s['physical_qubit_seconds'], s['physical_qubits']*s['runtime_seconds'])
            self.assertNotIn('compute_volume', s)
            self.assertEqual(flash.check_program(rebuilt), [])
            self.assertEqual(flash.build_layout(rebuilt), s)
            output = self.root / (mode+'.gltf')
            flash.write_gltf(rebuilt, output)
            self.assertTrue(json.loads(output.read_text())['nodes'])

    def test_cold_start_and_corruption_detection(self):
        p = self.build('t q[0];t q[0];tdg q[0];', factories=1)
        self.assertEqual([i[6] for i in p.physical_layout['injections']], [11,22,33])
        self.assertEqual(p.stats()['runtime_steps'], 35)
        q = copy.deepcopy(p)
        q.physical_layout['injections'][0][6] = 0
        self.assertTrue(check(q))
        q = copy.deepcopy(p)
        q.physical_layout['sites'][0]['rect'][0] = -1
        self.assertTrue(check(q))
        q = copy.deepcopy(p)
        q.physical_layout['mode'] = 'backside_vertical'
        self.assertTrue(check(q))

    def test_no_t_resolves_requested_budget_without_reserving_factories(self):
        for f in (1, 3, 6):
            p = self.build('h q[0];cx q[0],q[1];', factories=f)
            self.assertEqual(p.factory_mode, 'in_place' if f <= 2 else 'backside_horizontal')
            self.assertEqual(p.stats()['reserved_factory_count'], 0)
            self.assertEqual(p.stats()['t_count'], 0)
            self.assertEqual(check(p), [])

    def test_library_elaboration_uses_same_default(self):
        path = self.root / 'input.qasm'
        path.write_text('OPENQASM 2.0; include "qelib1.inc"; qreg q[2]; t q[0];t q[1];')
        pc = flash.partition(flash.parse(path))
        floorplan, cells, channels = flash.synthesize(pc, cache_dir=ROOT/'.miniflash-cache')
        p = flash.elaborate(floorplan, cells, events=pc.events, channels=channels,
                            factory=flash.get_factory('15-to-1'), factories=3)
        self.assertEqual(p.factory_mode, 'backside_horizontal')
        self.assertEqual(check(p), [])

    def test_multilayer_die_and_cell_coverage(self):
        body = 'h q[0];cx q[0],q[4];t q[4];cx q[1],q[3];h q[1];t q[1];cx q[4],q[1];tdg q[0];cx q[2],q[0];h q[2];t q[2];'
        for mode in MODES:
            p = self.build(body, n=5, factories=3, die_dims=(7,None), factory_mode=mode)
            self.assertEqual(check(p), [])
            p.physical_layout['cell_schedule'].pop()
            self.assertTrue(check(p))

    def test_route_bottleneck_serializes_available_factories(self):
        from miniflash.physical import _Scheduler
        layout = {'width':18, 'height':15, 'sites':[
            {'id':i, 'rect':[2,1+3*i,1,1], 'buffer':[1,1+3*i], 'port':[2,1+3*i], 'enabled':True}
            for i in range(4)], 'initial':[[i,[12,1+3*i]] for i in range(4)],
            'layers':[], 'parking':[], 'scratch':[0,0]}
        scheduler = _Scheduler(layout,4,11)
        scheduler.blocked.update((5,y) for y in range(15) if y != 3)
        plan = scheduler.run({0:[(i,i,False) for i in range(4)]})
        self.assertEqual(plan['peak_concurrent_injections'], 1)
        self.assertEqual(len({i[4] for i in plan['injections']}), 4)

    def test_magic_transport_parities(self):
        from miniflash.physical import pipes_of
        for mode in MODES:
            p = self.build(factories=4, factory_mode=mode)
            times = {i[0]:i[6] for i in p.physical_layout['injections']}
            saw_transport = False
            for pipe in pipes_of(p):
                if pipe.inj is None:continue
                merge = pipe.axis == 'J' and pipe.lo[2] == 2*(times[pipe.inj]+1)
                if not merge:
                    self.assertEqual(pipe.parity, 0)
                    saw_transport |= pipe.axis == 'J'
            self.assertTrue(saw_transport)

    def test_fast_and_external_factory_presets(self):
        for mode in MODES:
            for factory in ('t-cultivation', 'external'):
                p = self.build(factories=3, factory_mode=mode, factory=factory)
                self.assertEqual(check(p), [])
                self.assertEqual(p.stats()['t_count'], 4)

    def test_invalid_budgets_modes_and_legacy_json_are_rejected(self):
        for f in (0, -1, True, 1.5, '2'):
            with self.assertRaises(ValueError): resolve_mode(None, f)
        for mode in ('auto', 'backside', 'in_place_joint', 'unknown'):
            with self.assertRaises(ValueError): self.build(factory_mode=mode)
        for option in ('orientation', 'side_ports', 'fixed_columns'):
            with self.assertRaises(ValueError): self.build(**{option: True})
        data = self.build().to_dict()
        data.pop('factory_mode')
        with self.assertRaises(ValueError): flash.Program.from_dict(data)
        data = self.build().to_dict()
        data.pop('physical_layout')
        with self.assertRaises(ValueError): flash.Program.from_dict(data)

    def test_cli_lists_only_three_modes(self):
        output = subprocess.check_output([sys.executable, str(ROOT/'main.py'), '--help'], text=True)
        self.assertIn('{in_place,backside_vertical,backside_horizontal}', output)
        rejected = subprocess.run([sys.executable, str(ROOT/'main.py'),
            str(ROOT/'benchmarks/t-factory/t-factory-layer4.qasm'), '--factory-mode', 'auto'],
            capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        for f, mode in ((2, 'in_place'), (3, 'backside_horizontal')):
            result = subprocess.check_output([sys.executable, str(ROOT/'main.py'),
                str(ROOT/'benchmarks/t-factory/t-factory-layer4.qasm'), '--factories', str(f),
                '--cache-dir', str(ROOT/'.miniflash-cache')], text=True)
            self.assertIn('factory_mode='+mode, result)



if __name__ == '__main__':
    unittest.main()
