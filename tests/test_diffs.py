import unittest

from vader.diffs import filter_files, pack, split_files
from vader.github import parse_pr_ref


def git_file(path: str, body_lines: int = 3, hunks: int = 1) -> str:
    text = f"diff --git a/{path} b/{path}\nindex 111..222 100644\n--- a/{path}\n+++ b/{path}\n"
    for h in range(hunks):
        text += f"@@ -{h * 10 + 1},3 +{h * 10 + 1},{body_lines} @@\n" + "".join(f"+line {h}.{i}\n" for i in range(body_lines))
    return text


class SplitTests(unittest.TestCase):
    def test_git_diff_splits_per_file(self):
        diff = git_file("src/A.cs") + git_file("src/B.cs") + git_file("Views/C.cshtml")
        self.assertEqual([f.path for f in split_files(diff)], ["src/A.cs", "src/B.cs", "Views/C.cshtml"])

    def test_pasted_diff_without_git_headers(self):
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+a\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n+b\n"
        self.assertEqual([f.path for f in split_files(diff)], ["x.py", "y.py"])

    def test_deleted_file_uses_old_path(self):
        diff = "diff --git a/old.cs b/old.cs\ndeleted file mode 100644\n--- a/old.cs\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        self.assertEqual(split_files(diff)[0].path, "old.cs")

    def test_text_without_headers_is_one_piece(self):
        self.assertEqual(len(split_files("+just a line\n")), 1)
        self.assertEqual(split_files("   "), [])


class FilterTests(unittest.TestCase):
    def test_noise_is_dropped_and_code_is_kept(self):
        diff = "".join(git_file(p) for p in [
            "src/Orders/OrderService.cs", "package-lock.json", "wwwroot/js/site.min.js",
            "Migrations/20261018_Init.Designer.cs", "Migrations/AppDbContextModelSnapshot.cs",
            "src/bin/Debug/app.dll", "wwwroot/lib/bootstrap/bootstrap.css",
            "Scripts/pricing.ts", "wwwroot/js/pricing.js", "wwwroot/js/standalone.js",
        ]) + "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
        kept, skipped = filter_files(split_files(diff))
        self.assertEqual([f.path for f in kept],
                         ["src/Orders/OrderService.cs", "Scripts/pricing.ts", "wwwroot/js/standalone.js"])
        reasons = {s["path"]: s["reason"] for s in skipped}
        self.assertEqual(reasons["package-lock.json"], "lockfile")
        self.assertEqual(reasons["wwwroot/js/site.min.js"], "minified")
        self.assertEqual(reasons["Migrations/20261018_Init.Designer.cs"], "generated")
        self.assertEqual(reasons["Migrations/AppDbContextModelSnapshot.cs"], "generated")
        self.assertEqual(reasons["src/bin/Debug/app.dll"], "binary")
        self.assertEqual(reasons["wwwroot/lib/bootstrap/bootstrap.css"], "vendored library")
        self.assertEqual(reasons["wwwroot/js/pricing.js"], "compiled from TypeScript")
        self.assertEqual(reasons["logo.png"], "binary")


class PackTests(unittest.TestCase):
    def test_small_diff_is_one_part(self):
        files = split_files(git_file("a.cs") + git_file("b.cs"))
        self.assertEqual(len(pack(files, 10_000)), 1)

    def test_parts_respect_budget_and_keep_every_file(self):
        files = split_files("".join(git_file(f"f{i}.cs", body_lines=40) for i in range(10)))
        budget = 2000
        parts = pack(files, budget)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(sum(len(f.text) + 1 for f in p) <= budget for p in parts))
        self.assertEqual([f.path for p in parts for f in p], [f.path for f in files])

    def test_big_file_is_split_at_hunks_with_header_repeated(self):
        big = split_files(git_file("Big.cs", body_lines=30, hunks=6))
        parts = pack(big, 1200)
        pieces = [f for p in parts for f in p]
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(f.path.startswith("Big.cs (piece ") for f in pieces))
        self.assertTrue(all(f.text.startswith("diff --git a/Big.cs") for f in pieces))
        self.assertTrue(all(len(f.text) <= 1200 for f in pieces))
        joined = "".join(f.text for f in pieces)
        for h in range(6):
            self.assertIn(f"+line {h}.29", joined)

    def test_single_huge_hunk_is_cut_by_lines(self):
        huge = split_files(git_file("Huge.cs", body_lines=400))
        pieces = [f for p in pack(huge, 1500) for f in p]
        self.assertGreater(len(pieces), 3)
        self.assertTrue(all(len(f.text) <= 1500 for f in pieces))
        self.assertIn("+line 0.399", pieces[-1].text)


class PrRefTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_pr_ref("11", "o/r"), ("o/r", 11))
        self.assertEqual(parse_pr_ref("#11", "o/r"), ("o/r", 11))
        self.assertEqual(parse_pr_ref("x/y#4", "o/r"), ("x/y", 4))
        self.assertEqual(parse_pr_ref("<https://github.com/c0linhu1/options-desk/pull/9>", None),
                         ("c0linhu1/options-desk", 9))

    def test_bad_refs(self):
        with self.assertRaises(ValueError):
            parse_pr_ref("eleven", "o/r")
        with self.assertRaises(ValueError):
            parse_pr_ref("11", None)


if __name__ == "__main__":
    unittest.main()
