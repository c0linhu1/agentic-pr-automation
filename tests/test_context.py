import unittest
from unittest import mock

import httpx

from vader import llm
from vader.diffs import FileDiff, pack, pack_with_context
from vader.engine import Review
from vader.github import CONTEXT_MAX_CHARS, GitHubPoller, tests_summary


def diff(path: str, n: int = 5) -> FileDiff:
    return FileDiff(path, f"diff --git a/{path} b/{path}\n" + "".join(f"+line {i}\n" for i in range(n)))


class PackWithContextTests(unittest.TestCase):
    def test_context_is_kept_when_it_fits(self):
        files = [diff("a.cs"), diff("b.cs")]
        parts = pack_with_context(files, {"a.cs": "class A {}", "b.cs": "class B {}"}, budget=10_000)
        self.assertEqual(len(parts), 1)
        self.assertEqual([f.context for f in parts[0]], ["class A {}", "class B {}"])

    def test_context_never_adds_parts_and_the_largest_goes_first(self):
        files = [diff("a.cs", 20), diff("b.cs", 20)]
        budget = sum(f.size for f in files) + 300  # both diffs fit in one part, with a little room
        self.assertEqual(len(pack(files, budget)), 1)
        parts = pack_with_context(files, {"a.cs": "x" * 2000, "b.cs": "y" * 100}, budget)
        self.assertEqual(len(parts), 1)
        by_path = {f.path: f.context for f in parts[0]}
        self.assertEqual(by_path["a.cs"], "")          # the big one was dropped
        self.assertEqual(by_path["b.cs"], "y" * 100)   # the small one still fits

    def test_a_file_whose_context_doesnt_fit_is_not_split(self):
        f = diff("a.cs", 5)
        parts = pack([FileDiff(f.path, f.text, "z" * 50_000)], budget=2000)
        self.assertEqual(len(parts), 1)
        self.assertEqual((parts[0][0].text, parts[0][0].context), (f.text, ""))


class PromptTests(unittest.TestCase):
    def test_context_section_comes_before_the_diff(self):
        r = Review(source="github", validators=["qa"], title="t")
        r._parts = [[FileDiff("a.cs", "+x", "class A {}")]]
        prompt = r.prompt(0)
        self.assertIn("CONTEXT (the changed files in full", prompt)
        self.assertIn("=== a.cs ===\nclass A", prompt)
        self.assertLess(prompt.index("CONTEXT"), prompt.index("DIFF:"))

    def test_no_context_section_without_context(self):
        r = Review(source="slack", validators=["qa"])
        r._parts = [[FileDiff("a.cs", "+x")]]
        self.assertNotIn("CONTEXT", r.prompt(0))


class SharedPrefixTests(unittest.TestCase):
    def test_validators_share_everything_up_to_their_own_instructions(self):
        pr = "TITLE: t\n\nDIFF:\n+x"
        a = llm.messages("Review security.", pr)
        b = llm.messages("Review tests.", pr)
        self.assertEqual(a[0], b[0])  # identical system message
        shared = a[1]["content"].split(llm.TASK_HEADING)[0]
        self.assertTrue(b[1]["content"].startswith(shared))
        self.assertTrue(shared.startswith(pr))


class TestLayoutTests(unittest.TestCase):
    """The TESTS line: which kinds of code the repo has tests for, so QA can tell "no test" from "no test setup"."""

    REPO = ["src/App.Web/Scripts/cart.ts", "src/App.Web/Views/Orders/_Form.cshtml", "src/App.Billing/Invoice.cs",
            "tests/App.Billing.Tests/InvoiceTests.cs", "tests/App.Billing.Tests/Helpers/Fixtures.cs",
            "tests/App.Web.Tests/OrdersTests.cs", "tests/App.Web.Tests/data/orders.json", "README.md"]

    def test_names_the_tests_and_the_changed_code_that_has_none(self):
        line = tests_summary(self.REPO, ["src/App.Web/Scripts/cart.ts", "src/App.Web/Views/Orders/_Form.cshtml"])
        self.assertEqual(line, "TESTS: the repo's test files are 3 .cs files (tests/App.Billing.Tests, "
                               "tests/App.Web.Tests). There are no test files for .ts, which this PR changes.")

    def test_changed_code_with_tests_of_its_kind_isnt_flagged(self):
        repo = self.REPO + ["src/App.Web/Scripts/format.spec.ts"]
        line = tests_summary(repo, ["src/App.Web/Scripts/cart.tsx", "src/App.Billing/Invoice.cs"])
        self.assertIn("1 .ts file (src/App.Web/Scripts)", line)
        self.assertNotIn("There are no test files", line)  # .tsx is covered by .ts tests

    def test_recognises_common_test_naming(self):
        tests = ["pkg/api/test_orders.py", "svc/orders_test.go", "web/src/__tests__/cart.js", "lib/Cart.test.mjs",
                 "spec/models/user_spec.rb", "src/OrderServiceTest.java"]
        not_tests = ["src/Contest.cs", "src/latest.py", "docs/testing.md", "src/attestation.go"]
        line = tests_summary(tests + not_tests, [])
        for kind in ["1 .py", "1 .go", "2 .js", "1 .rb", "1 .java"]:
            self.assertIn(kind, line)
        self.assertNotIn(".cs", line)

    def test_no_tests_at_all_and_partial_listings_say_so(self):
        self.assertEqual(tests_summary(["src/a.py"], ["src/a.py"]), "TESTS: the repo has no test files.")
        self.assertIn("may be incomplete", tests_summary(self.REPO, [], truncated=True))

    def test_header_carries_it_and_a_failed_lookup_leaves_it_out(self):
        r = Review(source="github", validators=["qa"], title="t", test_layout="TESTS: the repo has no test files.")
        r._parts = [[FileDiff("a.ts", "+x")]]
        self.assertLess(r.prompt(0).index("TESTS:"), r.prompt(0).index("DIFF:"))
        self.assertNotIn("TESTS:", Review(source="slack", validators=["qa"], title="t").header())
        gh = mock.Mock(get=mock.Mock(return_value=mock.Mock(status_code=404)))
        self.assertEqual(GitHubPoller._test_layout(gh, "o/r", "abc", ["a.ts"]), "")

    def test_reads_the_tree_at_the_reviewed_commit(self):
        tree = {"truncated": False, "tree": [{"path": p, "type": "blob"} for p in self.REPO] + [
            {"path": "tests/App.Web.Tests", "type": "tree"}]}
        gh = mock.Mock(get=mock.Mock(return_value=mock.Mock(status_code=200, json=lambda: tree)))
        line = GitHubPoller._test_layout(gh, "o/r", "abc", ["src/App.Web/Scripts/cart.ts"])
        self.assertEqual(gh.get.call_args.args[0], "/repos/o/r/git/trees/abc")
        self.assertEqual(gh.get.call_args.kwargs["params"], {"recursive": "1"})
        self.assertIn("3 .cs files", line)


class FetchTests(unittest.TestCase):
    def test_fetches_source_files_and_skips_the_rest(self):
        files = [{"filename": "src/A.cs", "status": "modified"},
                 {"filename": "src/Old.cs", "status": "removed"},
                 {"filename": "package-lock.json", "status": "modified"},
                 {"filename": "src/Big.cs", "status": "added"},
                 {"filename": "src/Gone.cs", "status": "modified"}]

        def get(path, **kw):
            if path.endswith("/files"):
                return mock.Mock(json=lambda: files, raise_for_status=lambda: None)
            name = path.rsplit("/", 1)[-1]
            if name == "Big.cs":
                return mock.Mock(status_code=200, text="x" * (CONTEXT_MAX_CHARS + 1))
            if name == "Gone.cs":
                return mock.Mock(status_code=404, text="")
            return mock.Mock(status_code=200, text=f"// {name}")

        gh = mock.Mock(get=mock.Mock(side_effect=get))
        contents = GitHubPoller._file_contents(gh, "o/r", 16, "abc")
        self.assertEqual(contents, {"src/A.cs": "// A.cs"})
        fetched = [c.args[0] for c in gh.get.call_args_list if "/contents/" in c.args[0]]
        self.assertNotIn("/repos/o/r/contents/package-lock.json", fetched)
        self.assertNotIn("/repos/o/r/contents/src/Old.cs", fetched)

    def test_a_failure_listing_files_just_means_no_context(self):
        gh = mock.Mock(get=mock.Mock(side_effect=httpx.ConnectError("down")))
        self.assertEqual(GitHubPoller._file_contents(gh, "o/r", 16, "abc"), {})


if __name__ == "__main__":
    unittest.main()
