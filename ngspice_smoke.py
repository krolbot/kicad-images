#!/usr/bin/env python3
"""DC/transient regression for the two resistance and threshold orders.

Run inside an isolated image, from a copied fixture directory containing
.spiceinit with ngbehavior=ps. No TI models are involved in these tests.
"""
import argparse
import json
import math
from pathlib import Path
import subprocess


def expected_voltage(control, ron, roff, von, voff):
    fraction = min(1.0, max(0.0, (control - voff) / (von - voff)))
    weight = fraction * fraction * (3 - 2 * fraction)
    resistance = math.exp(math.log(roff) + weight * math.log(ron / roff))
    return 1000 / (1000 + resistance)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--expect-bug', action='store_true')
    args = parser.parse_args()
    root = Path.cwd()
    (root / 'switch-regression.json').unlink(missing_ok=True)
    cases = [(ron, roff, von, voff)
             for ron, roff in [(1e-3, 1e8), (1e8, 1e-3), (10, 10)]
             for von, voff in [(0.8, 0.2), (0.2, 0.8)]]
    library = ['VBIAS BIAS 0 1']
    for i, (ron, roff, von, voff) in enumerate(cases):
        library.extend([
            f'S{i} BIAS N{i} CTRL 0 M{i}',
            f'R{i} N{i} 0 1k',
            f'.model M{i} VSWITCH (Ron={ron} Roff={roff} Von={von} Voff={voff})',
        ])
    (root / 'switch-fixture.lib').write_text('\n'.join(library) + '\n')
    results = []
    failures = []
    for analysis, source, command in [
        ('dc', 'VCTRL CTRL 0 0', 'dc VCTRL -0.1 1.1 0.025'),
        ('transient', 'VCTRL CTRL 0 PWL(0 -0.1 10u 1.1)', 'tran 50n 10u 0 50n'),
    ]:
        output = root / f'switch-{analysis}.csv'
        output.unlink(missing_ok=True)
        netlist = root / f'switch-{analysis}.cir'
        netlist.write_text(
            '* VSWITCH resistance/threshold-order regression\n' + source +
            '\n.include switch-fixture.lib\n'
            '.options reltol=1e-9 abstol=1e-15 vntol=1e-12 gmin=1e-15\n'
            '.control\nset wr_singlescale\nset wr_vecnames\n' + command +
            '\nwrdata ' + output.name + ' v(CTRL) ' +
            ' '.join(f'v(N{i})' for i in range(len(cases))) +
            '\nquit\n.endc\n.end\n'
        )
        run = subprocess.run(['ngspice', '-b', str(netlist)],
                             capture_output=True, text=True, timeout=30)
        (root / f'switch-{analysis}.log').write_text(run.stdout + run.stderr)
        assert run.returncode == 0, f'ngspice rc={run.returncode}: {run.stdout}{run.stderr}'
        assert not any(marker in (run.stdout + run.stderr).lower() for marker in
                       ['simulation interrupted', 'timestep too small', 'simulation(s) aborted'])
        lines = output.read_text().splitlines()
        rows = [list(map(float, line.split())) for line in lines[1:] if line.strip()]
        assert len(rows) >= 40, 'Incomplete sweep'
        assert math.isclose(rows[0][1], -0.1, abs_tol=1e-8)
        assert math.isclose(rows[-1][1], 1.1, abs_tol=1e-8)
        errors = [0.0] * len(cases)
        for row in rows:
            assert len(row) == len(cases) + 2 and all(map(math.isfinite, row))
            control = row[1]
            for i, params in enumerate(cases):
                error = abs(row[i + 2] - expected_voltage(control, *params))
                errors[i] = max(errors[i], error)
                if error > 2e-6:
                    failures.append(dict(analysis=analysis, case=i, control=control,
                                         actual=row[i + 2], expected=expected_voltage(control, *params)))
        results.append(dict(analysis=analysis, rows=len(rows), max_absolute_error_V=errors))
    report = dict(expect_bug=args.expect_bug, cases=cases, results=results,
                  failures=len(failures), first_failure=failures[:1])
    (root / 'switch-regression.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if args.expect_bug:
        assert failures, 'Expected baseline bug was not reproduced'
        assert all(cases[entry['case']][0] > cases[entry['case']][1] for entry in failures), \
            'Unexpected failure outside inverse-resistance cases'
    else:
        assert not failures, 'VSWITCH DC/transient regression failed'


if __name__ == '__main__':
    main()
