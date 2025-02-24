# SPDX-License-Identifier: AGPL-3.0

import os
import sys
import subprocess
import uuid
import json
import argparse
import re

from timeit import default_timer as timer

from crytic_compile import cryticparser
from crytic_compile import CryticCompile, InvalidCompilation

from .utils import color_good, color_warn
from .sevm import *

sys.set_int_max_str_digits(0)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog='halmos', epilog='For more information, see https://github.com/a16z/halmos')

    parser.add_argument('target', metavar='TARGET_DIRECTORY', nargs='?', default=os.getcwd(), help='source root directory (default: current directory)')
    parser.add_argument('--contract', metavar='CONTRACT_NAME', help='run tests in the given contract only')
    parser.add_argument('--function', metavar='FUNCTION_NAME_PREFIX', default='test', help='run tests matching the given prefix only (default: %(default)s)')

    parser.add_argument('--loop', metavar='MAX_BOUND', type=int, default=2, help='set loop unrolling bounds (default: %(default)s)')
    parser.add_argument('--width', metavar='MAX_WIDTH', type=int, help='set the max number of paths')
    parser.add_argument('--depth', metavar='MAX_DEPTH', type=int, help='set the max path length')
    parser.add_argument('--array-lengths', metavar='NAME1=LENGTH1,NAME2=LENGTH2,...', help='set the length of dynamic-sized arrays including bytes and string (default: loop unrolling bound)')

    parser.add_argument('--no-smt-add',          action='store_true', help='do not interpret `+`')
    parser.add_argument('--no-smt-sub',          action='store_true', help='do not interpret `-`')
    parser.add_argument('--no-smt-mul',          action='store_true', help='do not interpret `*`')
    parser.add_argument(   '--smt-div',          action='store_true', help=       'interpret `/`')
    parser.add_argument(   '--smt-div-by-const', action='store_true', help=       'interpret division by constant')
    parser.add_argument(   '--smt-mod-by-const', action='store_true', help=       'interpret constant modulo')
    parser.add_argument(   '--smt-exp-by-const', metavar='N', type=int, default=2, help='interpret constant power up to N (default: %(default)s)')

    parser.add_argument('--solver-timeout-branching', metavar='TIMEOUT', type=int, default=1000, help='set timeout (in milliseconds) for solving branching conditions (default: %(default)s)')
    parser.add_argument('--solver-timeout-assertion', metavar='TIMEOUT', type=int, default=60000, help='set timeout (in milliseconds) for solving assertion violation conditions (default: %(default)s)')
    parser.add_argument('--solver-subprocess', action='store_true', help='run an extra solver in subprocess for unknown')

    parser.add_argument('-v', '--verbose', action='count', default=0, help='increase verbosity levels: -v, -vv, -vvv, -vvvv')
    parser.add_argument('--debug', action='store_true', help='run in debug mode')
    parser.add_argument('--log', metavar='LOG_FILE_PATH', help='log individual execution steps in JSON')
    parser.add_argument('--print-revert', action='store_true', help='print reverting paths in verbose mode')

    cryticparser.init(parser)

    return parser.parse_args()

def add_srcmap(ops: List[Opcode], srcmap: List[str], srcs: Dict) -> None:
    fpath = {} # file id -> file path
    for src in srcs:
        fpath[srcs[src]['id']] = src
    #   print(src, srcs[src]['id'])

    start, length, fileid, jump, mdepth = 0, 0, 0, '-', 0
    for idx, sm in enumerate(srcmap):
        arr = sm.split(':') + ['']*5
        start  = int(arr[0]) if arr[0] != '' else start
        length = int(arr[1]) if arr[1] != '' else length
        srcidx = int(arr[2]) if arr[2] != '' else srcidx
        jump   =     arr[3]  if arr[3] != '' else jump
        mdepth = int(arr[4]) if arr[4] != '' else mdepth

        if srcidx in fpath:
            with open(f'{args.target}/{fpath[srcidx]}') as f:
                f.seek(start)
                srctext = repr(f.read(length))
        else:
            srctext = '<generated>'

        ops[idx].sm = SrcMap(srctext, jump, mdepth)

def setup(
    hexcode: str,
    abi: Dict,
    srcmap: List[str],
    srcs: Dict,
    args: argparse.Namespace,
    setup_sig: str,
    options: Dict
) -> Exec:
    # bytecode
    (ops, code) = decode(hexcode)
    pgm = ops_to_pgm(ops)
    add_srcmap(ops, srcmap, srcs)

    # solver
    solver = SolverFor('QF_AUFBV') # quantifier-free bitvector + array theory; https://smtlib.cs.uiowa.edu/logics.shtml
    solver.set(timeout=args.solver_timeout_branching)

    # storage
    storage = {}

    # caller
    caller = BitVec('msg_sender', 256)
    solver.add(Extract(255, 160, caller) == BitVecVal(0, 96))

    # this
    this = BitVec('this_address', 256)
    solver.add(Extract(255, 160, this) == BitVecVal(0, 96))

    # run setup if any

    sevm = SEVM(options)

    setup_ex = sevm.mk_exec(
        pgm       = { this: pgm },
        code      = { this: code },
        storage   = { this: storage },
        balance   = { this: con(0) },
        calldata  = [None] * 4,
        callvalue = con(0),
        caller    = caller,
        this      = this,
        symbolic  = False,
        solver    = solver,
    )

    if setup_sig:
        wstore(setup_ex.calldata, 0, 4, BitVecVal(setup_sig, 32))

        (setup_exs, setup_steps) = sevm.run(setup_ex)

        if len(setup_exs) != 1:
            if args.debug: print('\n'.join(map(str, setup_exs)))
            raise ValueError('multiple paths exist in setUp()')

        setup_ex = setup_exs[0]

        setup_opcode = setup_ex.pgm[setup_ex.this][setup_ex.pc].op[0]
        if (setup_opcode != 'STOP' and setup_opcode != 'RETURN') or setup_ex.failed: raise ValueError('setUp() failed')

        if args.verbose >= 2:
            print(setup_ex)

    return setup_ex

def run(
    setup_ex: Exec,
    abi: Dict,
    funname: str,
    funsig: str,
    funselector: str,
    arrlen: Dict,
    args: argparse.Namespace,
    options: Dict
) -> int:
    #
    # calldata
    #

    f_cd = Function('cd', BitVecSort(256), BitVecSort(8))
    cdsize = 10000
    cd = []
    for i in range(cdsize):
        cd.append(f_cd(con(i)))

    wstore(cd, 0, 4, BitVecVal(funselector, 32))

    dyn_param_size = []

    for item in abi:
        if item['type'] == 'function' and item['name'] == funname:
            tba = []
            offset = 0
            for param in item['inputs']:
                param_name = param['name']
                param_type = param['type']
                if param_type == 'tuple':
                    raise ValueError('Not supported', param_type) # TODO: support struct types
                elif param_type == 'bytes' or param_type == 'string':
                    tba.append((4+offset, param)) # wstore(cd, 4+offset, 32, BitVecVal(<?offset?>, 256))
                    offset += 32
                elif param_type.endswith('[]'):
                    raise ValueError('Not supported variable sized arrays', param_type)
                else:
                    match = re.search(r'(u?int[0-9]*|address|bool|bytes[0-9]+)(\[([0-9]+)\])?', param_type)
                    if not match: raise ValueError('Unknown type', param_type)
                    typ = match.group(1)
                    dim = match.group(3)
                    if dim: # array
                        for idx in range(int(dim)):
                            wstore(cd, 4+offset, 32, BitVec(f'p_{param_name}[{idx}]_{typ}', 256))
                            offset += 32
                    else: # primitive
                        wstore(cd, 4+offset, 32, BitVec(f'p_{param_name}_{typ}', 256))
                        offset += 32

            for loc_param in tba:
                loc   = loc_param[0]
                param = loc_param[1]
                param_name = param['name']
                param_type = param['type']

                if param_name not in arrlen:
                    size = args.loop
                    if args.debug: print(f'warn: size of {param_name} not given, using default value {size}')
                else:
                    size = arrlen[param_name]

                dyn_param_size.append(f'|{param_name}|={size}')

                if param_type == 'bytes' or param_type == 'string':
                    # head
                    wstore(cd, loc, 32, BitVecVal(offset, 256))
                    # tail
                    size_pad_right = int((size + 31) / 32) * 32
                    wstore(cd, 4+offset, 32, BitVecVal(size, 256))
                    offset += 32
                    if size_pad_right > 0:
                        wstore(cd, 4+offset, size_pad_right, BitVec(f'p_{param_name}_{param_type}', 8*size_pad_right))
                        offset += size_pad_right
                else:
                    raise ValueError('not feasible')

    #
    # callvalue
    #

    callvalue = BitVec('msg_value', 256)

    #
    # balance
    #

    balance = BitVec('this_balance', 256)

    #
    # run
    #

    start = timer()

    sevm = SEVM(options)

    solver = SolverFor('QF_AUFBV')
    solver.set(timeout=args.solver_timeout_branching)
    solver.add(setup_ex.solver.assertions())

    (exs, steps) = sevm.run(Exec(
        pgm       = setup_ex.pgm.copy(), # shallow copy
        code      = setup_ex.code.copy(), # shallow copy
        storage   = deepcopy(setup_ex.storage),
        balance   = { setup_ex.this: sevm.arith('ADD', balance, callvalue) },
        #
        calldata  = cd,
        callvalue = callvalue,
        caller    = setup_ex.caller,
        this      = setup_ex.this,
        #
        pc        = 0,
        st        = State(),
        jumpis    = {},
        output    = None,
        symbolic  = True,
        #
        solver    = solver,
        path      = deepcopy(setup_ex.path),
        #
        log       = deepcopy(setup_ex.log),
        cnts      = deepcopy(setup_ex.cnts),
        sha3s     = deepcopy(setup_ex.sha3s),
        storages  = deepcopy(setup_ex.storages),
        calls     = deepcopy(setup_ex.calls),
        failed    = setup_ex.failed,
        error     = setup_ex.error,
    ))

    # check assertion violations
    normal = 0
    models = []
    stuck = []
    for idx, ex in enumerate(exs):
        opcode = ex.pgm[ex.this][ex.pc].op[0]
        if opcode == 'STOP' or opcode == 'RETURN':
            if ex.failed:
                gen_model(args, models, idx, ex)
            else:
                normal += 1
        elif opcode == 'REVERT':
            # Panic(1) # bytes4(keccak256("Panic(uint256)")) + bytes32(1)
            if ex.output == int('4e487b71' + '0000000000000000000000000000000000000000000000000000000000000001', 16): # 152078208365357342262005707660225848957176981554335715805457651098985835139029979365377
                gen_model(args, models, idx, ex)
        else:
            stuck.append((opcode, idx, ex))

    end = timer()

    passed = (normal > 0 and len(models) == 0 and len(stuck) == 0)
    if passed:
        passfail = color_good('[PASS]')
    else:
        passfail = color_warn('[FAIL]')

    # print result
    print(f"{passfail} {funsig} (paths: {normal}/{len(exs)}, time: {end - start:0.2f}s, bounds: [{', '.join(dyn_param_size)}])")
    for model, idx, ex in models:
        if model:
            print(color_warn('Counterexample: ' + str_model(model, args)))
        else:
            print(color_warn('Counterexample: unknown'))
        if args.verbose >= 1:
            print(f'# {idx+1} / {len(exs)}')
            print(ex)
    for opcode, idx, ex in stuck:
        print(color_warn('Not supported: ' + opcode + ' ' + ex.error))
        if args.verbose >= 1:
            print(f'# {idx+1} / {len(exs)}')
            print(ex)

    # print post-states
    if args.verbose >= 2:
        for idx, ex in enumerate(exs):
            if args.print_revert or (ex.pgm[ex.this][ex.pc].op[0] != 'REVERT' and not ex.failed):
                print(f'# {idx+1} / {len(exs)}')
                print(ex)

    # log steps
    if args.log:
        with open(args.log, 'w') as json_file:
            json.dump(steps, json_file)

    # exitcode
    if passed:
        return 0
    else:
        return 1

def gen_model(args: argparse.Namespace, models: List, idx: int, ex: Exec) -> None:
    res = ex.solver.check()
    if res == sat: model = ex.solver.model()
    if res == unknown:
        sol2 = SolverFor('QF_AUFBV', ctx=Context())
        sol2.set(timeout=args.solver_timeout_assertion)
        sol2.from_string(ex.solver.sexpr())
        res = sol2.check()
        if res == sat: model = sol2.model()
    if res == sat and not is_valid_model(model):
        ctx = Context()
        sol3 = Solver(ctx=ctx)
        sol3.set(timeout=args.solver_timeout_assertion)
        sol3.from_string(ex.solver.sexpr())
        x = BitVec('x', 256, ctx)
        y = BitVec('y', 256, ctx)
    #   zero = BitVecVal(0, 256, ctx)
    #   one  = BitVecVal(1, 256, ctx)
    #   two  = BitVecVal(2, 256, ctx)
        evm_div = f_div.translate(ctx)
        evm_mod = f_mod.translate(ctx)
    #   evm_exp = f_exp.translate(ctx)
        # axiomatization
        sol3.add(ForAll([x, y], ULE(evm_div(x, y), x)))                 # (x / y) <= x
        sol3.add(ForAll([x, y], ULE(evm_mod(x, y), y)))                 # (x % y) <= y
    #   sol3.add(ForAll([x, y], Or(y == zero, ULT(evm_mod(x, y), y))))  # (x % y) < y if y != 0
    #   #
    #   sol3.add(ForAll([x], evm_div(x, zero) == zero))         # x / 0 == 0    # evm-specific
    #   sol3.add(ForAll([x], evm_div(x, one) == x))             # x / 1 == x
    #   sol3.add(ForAll([x], evm_div(x, two) == LShR(x, 1)))    # x / 2 == x >> 1
    #   #
    #   sol3.add(ForAll([x], evm_mod(x, zero) == zero))         # x % 0 == 0    # evm-specific
    #   sol3.add(ForAll([x], evm_mod(x, one) == zero))          # x % 1 == 0
    #   sol3.add(ForAll([x], evm_mod(x, two) == x & one))       # x % 2 == x & 1
    #   #
    #   sol3.add(ForAll([x], evm_exp(x, zero) == one))          # x ** 0 == 1   # 0 ** 0 == 1
    #   sol3.add(ForAll([x], evm_exp(x, one) == x))             # x ** 1 == x
    #   sol3.add(ForAll([x], evm_exp(x, two) == x * x))         # x ** 2 == x * x
    #   #
        res = sol3.check()
        if res == sat: model = sol3.model()
    if res == unknown and args.solver_subprocess:
        fname = f'/tmp/{uuid.uuid4().hex}.smt2'
        if args.verbose >= 4: print(f'z3 -smt2 {fname}')
        with open(fname, 'w') as f:
            f.write('(set-logic QF_AUFBV)\n')
            f.write(ex.solver.to_smt2())
        res_str = subprocess.run(['z3', fname], capture_output=True, text=True).stdout.strip()
        if args.verbose >= 4: print(res_str)
        if res_str == 'unsat':
            res = unsat
    if res == unsat:
        return
    if res == sat:
        if is_valid_model(model):
            models.append((model, idx, ex))
        else:
            models.append((None, idx, ex))
    else:
        models.append((None, idx, ex))

def is_valid_model(model) -> bool:
    for decl in model:
        inter = model[decl]
        if str(decl).startswith('evm_'):
            return False
    return True

def str_model(model, args: argparse.Namespace) -> str:
    def select(var):
        name = str(var)
        if name.startswith('p_'): return True
        elif args.verbose >= 1:
            if name.startswith('storage') or name.startswith('msg_') or name.startswith('this_'): return True
        return False
    if args.debug:
        return str(model)
    else:
        return '[' + ', '.join(sorted(map(lambda decl: f'{decl} = {model[decl]}', filter(select, model)))) + ']'

def main() -> int:
    #
    # z3 global options
    #

    set_option(max_width=240)
    set_option(max_lines=100000000)
#   set_option(max_depth=1000)

    #
    # command line arguments
    #

    args = parse_args()

    options = {
        'verbose': args.verbose,
        'debug': args.debug,
        'log': args.log,
        'add': not args.no_smt_add,
        'sub': not args.no_smt_sub,
        'mul': not args.no_smt_mul,
        'div': args.smt_div,
        'divByConst': args.smt_div_by_const,
        'modByConst': args.smt_mod_by_const,
        'expByConst': args.smt_exp_by_const,
        'timeout': args.solver_timeout_branching,
    }

    if args.width is not None:
        options['max_width'] = args.width

    if args.depth is not None:
        options['max_depth'] = args.depth

    if args.loop is not None:
        options['max_loop'] = args.loop

    arrlen = {}
    if args.array_lengths:
        for assign in [x.split('=') for x in args.array_lengths.split(',')]:
            name = assign[0].strip()
            size = assign[1].strip()
            arrlen[name] = int(size)

    #
    # compile
    #

    try:
        cryticCompile = CryticCompile(**vars(args))
    except InvalidCompilation as e:
        raise ValueError('Parse error', e)

    #
    # run
    #

    total_passed = 0
    total_failed = 0

    for compilation_id, compilation_unit in cryticCompile.compilation_units.items():

        for filename in sorted(compilation_unit.filenames):
            contracts_names = compilation_unit.filename_to_contracts[filename]
            source_unit = compilation_unit.source_units[filename]

            if args.contract:
                if args.contract not in contracts_names: continue
                contracts = [args.contract]
            else:
                contracts = sorted(contracts_names)

            for contract in contracts:
                hexcode = source_unit.bytecodes_runtime[contract]
                srcmap = source_unit.srcmaps_runtime[contract]
                srcs = {}
                abi = source_unit.abis[contract]
                methodIdentifiers = source_unit.hashes(contract)

                funsigs = [funsig for funsig in methodIdentifiers if funsig.startswith(args.function)]

                setup_sig = methodIdentifiers.get('setUp()')

                if funsigs:
                    num_passed = 0
                    num_failed = 0
                    print(f'\nRunning {len(funsigs)} tests for {filename.short}:{contract}')
                    setup_ex = setup(hexcode, abi, srcmap, srcs, args, setup_sig, options)
                    for funsig in funsigs:
                        funselector = methodIdentifiers[funsig]
                        funname = funsig.split('(')[0]
                        exitcode = run(setup_ex, abi, funname, funsig, funselector, arrlen, args, options)
                        if exitcode == 0:
                            num_passed += 1
                        else:
                            num_failed += 1
                    print(f'Symbolic test result: {num_passed} passed; {num_failed} failed')
                    total_passed += num_passed
                    total_failed += num_failed

    if (total_passed + total_failed) == 0:
        raise ValueError('No matching tests found', args.contract, args.function)

    # exitcode
    if total_failed == 0:
        return 0
    else:
        return 1

if __name__ == '__main__':
    sys.exit(main())
