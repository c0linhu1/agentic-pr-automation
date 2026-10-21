import unittest
from unittest import mock

from vader.diffs import FileDiff, pack, pack_with_context
from vader.engine import Review
from vader.github import CONTEXT_MAX_CHARS, GitHubPoller


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
        r._parts = [[FileDiff("a.cs", "+x", "class A { int Price() => Greeks(); }")]]
        prompt = r.prompt(0)
        self.assertIn("CONTEXT (full contents of changed files", prompt)
        self.assertIn("=== a.cs ===\nclass A", prompt)
        self.assertLess(prompt.index("CONTEXT"), prompt.index("DIFF:"))

    def test_no_context_section_without_context(self):
        r = Review(source="slack", validators=["qa"])
        r._parts = [[FileDiff("a.cs", "+x")]]
        self.assertNotIn("CONTEXT", r.prompt(0))


class SharedPrefixTests(unittest.TestCase):
    def test_validators_share_everything_up_to_their_own_instructions(self):
        from vader import llm
        pr = "TITLE: t\n\nDIFF:\n+x"
        a = llm.messages("Review security.", pr)
        b = llm.messages("Review tests.", pr)
        self.assertEqual(a[0], b[0])  # identical system message
        shared = a[1]["content"].split(llm.TASK_HEADING)[0]
        self.assertTrue(b[1]["content"].startswith(shared))
        self.assertTrue(shared.startswith(pr))


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
        import httpx
        gh = mock.Mock(get=mock.Mock(side_effect=httpx.ConnectError("down")))
        self.assertEqual(GitHubPoller._file_contents(gh, "o/r", 16, "abc"), {})


if __name__ == "__main__":
    unittest.main()
