"""What the parsers make of output real tools actually produced.

Every sample in this file was copied out of a run of the built-in stack, escape
codes and box drawing included. That is the point: a parser tested against
output invented for the test passes forever and works never.
"""

from __future__ import annotations

from pathlib import Path

from applace.diagnostics import parse, parse_bundler, parse_eslint, parse_npm, parse_tsc

TSC_OUTPUT = """\

> team-dashboard@0.0.0 typecheck
> tsc --noEmit

src/App.tsx(1,7): error TS2322: Type 'number' is not assignable to type 'string'.
src/App.tsx(3,9): error TS6133: 'unused' is declared but its value is never read.
"""

ESLINT_OUTPUT = """\

> team-dashboard@0.0.0 lint
> eslint .


/private/tmp/app/src/App.tsx
  3:9  error  'unused' is assigned a value but never used  @typescript-eslint/no-unused-vars

✖ 1 problem (1 error, 0 warnings)
"""

ROLLDOWN_UNRESOLVED = """\
vite v8.3.0 building client environment for production...
transforming...
✓ 16 modules transformed.
✗ Build failed in 31ms
error during build:
Build failed with 1 error:

[UNRESOLVED_IMPORT] Could not resolve './nope' in src/App.tsx
   ╭─[ src/App.tsx:1:25 ]
   │
 1 │ import { missing } from "./nope";
   │                         ────┬───
   │                             ╰───── Module not found.
───╯

    at aggregateBindingErrorsIntoJsError (file:///app/node_modules/rolldown/dist/shared/error.mjs:48:18)
    at async CAC.<anonymous> (file:///app/node_modules/vite/dist/node/cli.js:780:3) {
  errors: [Getter/Setter]
}
"""

ROLLDOWN_SYNTAX = """\
error during build:
Build failed with 1 error:

[builtin:vite-transform] Unexpected token. Did you mean `{'}'}` or `&rbrace;`?
   ╭─[ src/App.tsx:3:1 ]
   │
 3 │ }
───╯
"""

ESBUILD_OUTPUT = """\
✘ [ERROR] Could not resolve "./missing"

    src/main.tsx:2:20:
      2 │ import x from "./missing"
"""


def test_a_tsc_error_keeps_its_file_line_column_and_code() -> None:
    found = parse_tsc(TSC_OUTPUT)
    assert [(d.file, d.line, d.column, d.code) for d in found] == [
        ("src/App.tsx", 1, 7, "TS2322"),
        ("src/App.tsx", 3, 9, "TS6133"),
    ]
    assert found[0].message == "Type 'number' is not assignable to type 'string'."


def test_the_tsc_pretty_format_parses_too() -> None:
    found = parse_tsc("src/App.tsx:1:7 - error TS2322: Type 'number' is wrong.")
    assert (found[0].file, found[0].line, found[0].column) == ("src/App.tsx", 1, 7)


def test_npms_own_preamble_is_not_mistaken_for_a_diagnostic() -> None:
    assert len(parse_tsc(TSC_OUTPUT)) == 2


def test_eslint_attributes_a_problem_to_the_heading_above_it() -> None:
    found = parse_eslint(ESLINT_OUTPUT, Path("/private/tmp/app"))
    assert len(found) == 1
    assert found[0].file == "src/App.tsx"
    assert (found[0].line, found[0].column) == (3, 9)
    assert found[0].code == "@typescript-eslint/no-unused-vars"
    assert found[0].message == "'unused' is assigned a value but never used"


def test_eslints_absolute_paths_come_back_relative_to_the_app() -> None:
    found = parse_eslint(ESLINT_OUTPUT, Path("/private/tmp/app"))
    assert not Path(str(found[0].file)).is_absolute()


def test_an_unresolved_import_points_at_the_import() -> None:
    found = parse_bundler(ROLLDOWN_UNRESOLVED)
    assert len(found) == 1
    assert found[0].code == "UNRESOLVED_IMPORT"
    assert (found[0].file, found[0].line, found[0].column) == ("src/App.tsx", 1, 25)


def test_rolldowns_stack_through_node_modules_is_not_reported_as_an_error() -> None:
    found = parse_bundler(ROLLDOWN_UNRESOLVED)
    assert all("node_modules" not in d.message for d in found)


def test_a_syntax_error_in_the_build_points_at_the_line() -> None:
    found = parse_bundler(ROLLDOWN_SYNTAX)
    assert (found[0].file, found[0].line) == ("src/App.tsx", 3)
    assert "Unexpected token" in found[0].message


def test_esbuilds_own_format_still_parses() -> None:
    found = parse_bundler(ESBUILD_OUTPUT)
    assert (found[0].file, found[0].line, found[0].column) == ("src/main.tsx", 2, 20)
    assert found[0].message == 'Could not resolve "./missing"'


def test_npm_reports_the_sentence_and_not_the_debug_log_path() -> None:
    found = parse_npm(
        "npm error code E404\n"
        "npm error 404 Not Found - GET https://registry.npmjs.org/nope-nope\n"
        "npm error A complete log of this run can be found in: /Users/x/.npm/_logs/x.log\n"
    )
    assert len(found) == 1
    assert "404 Not Found" in found[0].message


def test_output_no_parser_understands_still_says_something() -> None:
    found = parse("build", "everything is on fire\nand also broken\n")
    assert len(found) == 1
    assert "on fire" in found[0].message


def test_a_stage_that_printed_nothing_is_reported_as_such() -> None:
    found = parse("build", "")
    assert "without printing anything" in found[0].message


def test_a_flood_of_errors_is_truncated_and_says_so() -> None:
    output = "\n".join(
        f"src/f{i}.ts({i},1): error TS2322: nope." for i in range(1, 200)
    )
    found = parse("typecheck", output)
    assert len(found) < 60
    assert "more of the same kind" in found[-1].message
