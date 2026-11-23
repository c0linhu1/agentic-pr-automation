import unittest
from unittest import mock

from vader import config
from vader.diffs import (filter_files, generated_source_changed, has_generated_banner, needs_person, pack, split_files,
                         unsafe_skips)


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
            "Scripts/cart.ts", "wwwroot/js/cart.js", "wwwroot/js/standalone.js",
        ]) + "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
        kept, skipped = filter_files(split_files(diff))
        self.assertEqual([f.path for f in kept],
                         ["src/Orders/OrderService.cs", "Scripts/cart.ts", "wwwroot/js/standalone.js"])
        reasons = {s["path"]: s["reason"] for s in skipped}
        self.assertEqual(reasons["package-lock.json"], "lockfile changed without package.json")
        self.assertEqual(reasons["wwwroot/js/site.min.js"], "minified")
        self.assertEqual(reasons["Migrations/20261018_Init.Designer.cs"], "generated")
        self.assertEqual(reasons["Migrations/AppDbContextModelSnapshot.cs"], "generated")
        self.assertEqual(reasons["src/bin/Debug/app.dll"], "executable or archive")
        self.assertEqual(reasons["wwwroot/lib/bootstrap/bootstrap.css"], "vendored library")
        self.assertEqual(reasons["wwwroot/js/cart.js"], "compiled from TypeScript")
        self.assertEqual(reasons["logo.png"], "image, font or document")

    def test_other_ecosystems_noise_is_dropped(self):
        noise = {"uv.lock": "lockfile changed without pyproject.toml", "ios/Podfile.lock": "lockfile",
                 "api/user_pb2.py": "generated",
                 "app/lib/user.g.dart": "generated", "target/site/index.html": "build output",
                 "web/.next/server/page.js": "build output", "pkg/__pycache__/x.py": "build output",
                 "src/App/bin/Release/net10.0/appsettings.json": "build output",
                 "frontend/dist/app.js": "build output", "packages/a/dist/index.js": "build output",
                 "packages/a/node_modules/x/index.js": "build output",
                 "vendor/github.com/pkg/errors/errors.go": "vendored library",
                 "ios/Pods/Alamofire/Session.swift": "vendored library",
                 "third_party/zlib/zlib.h": "vendored library", "lib/foo.jar": "executable or archive"}
        # Folder names that are also ordinary source folders, deeper down, or files a build doesn't write.
        code = ["ios/Podfile", "bin/setup", "bin/cli.js", "src/vendor/vendor_service.py", "src/coverage/policy.py",
                "build/package/Dockerfile", "build/webpack.config.js", "src/main/java/com/acme/build/Tool.java",
                "src/out/report.py", "app/services/target/rules.ts", "src/pods/scheduler.go"]
        kept, skipped = filter_files(split_files("".join(git_file(p) for p in [*noise, *code])))
        self.assertEqual([f.path for f in kept], code)
        self.assertEqual({s["path"]: s["reason"] for s in skipped}, noise)

    def test_only_skips_that_cant_hide_hand_written_code_are_safe(self):
        skipped = [{"path": "a.lock", "reason": "lockfile"}, {"path": "x.Designer.cs", "reason": "generated"},
                   {"path": "Helpers.generated.cs", "reason": "generated"},
                   {"path": "b.lock", "reason": "lockfile changed without package.json"},
                   {"path": "vendor/a.go", "reason": "vendored library"},
                   {"path": "x/mock.go", "reason": "marked as generated"}, {"path": "dist/a.js", "reason": "build output"}]
        banner = lambda path: path == "x.Designer.cs"  # noqa: E731 - the one whose first lines were checked
        self.assertEqual([s["path"] for s in unsafe_skips(skipped, banner)],
                         ["Helpers.generated.cs", "b.lock", "vendor/a.go", "x/mock.go", "dist/a.js"])

    def test_a_new_generated_file_needs_its_source_in_the_change(self):
        cases = [  # (generated file, files reviewed in the change, has its source)
            ("Migrations/2026_Init.Designer.cs", {"Migrations/2026_Init.cs"}, True),
            ("Properties/Resources.Designer.cs", {"Properties/Resources.resx"}, True),
            ("Migrations/AppDbContextModelSnapshot.cs", {"Migrations/2026_Init.cs"}, True),
            ("api/user_pb2.py", {"protos/user.proto"}, True),
            ("api/user_pb2_grpc.py", {"protos/user.proto"}, True),
            ("gen/user_grpc.pb.go", {"protos/user.proto"}, True),
            ("api/user_pb2.py", {"protos/billing.proto"}, False),  # some other .proto
            ("lib/user.g.dart", {"lib/user.dart"}, True),
            ("src/Helpers.g.cs", {"src/Other.cs"}, False),
            ("src/Helpers.g.cs", {"tests/Helpers.cs"}, False),  # same name, different folder
            ("api/user_pb2.py", {"api/user.py"}, False),
        ]
        for path, reviewed, expected in cases:
            with self.subTest(path=path, reviewed=reviewed):
                self.assertEqual(generated_source_changed(path, reviewed), expected)

    def test_binaries_that_can_run_and_minified_code_need_a_person(self):
        unknown = "diff --git a/data.bin b/data.bin\nBinary files a/data.bin and b/data.bin differ\n"
        diff = "".join(git_file(p) for p in ["logo.png", "fonts/a.woff2", "app.js.map", "lib/x.dll", "tools/y.jar",
                                             "keys/app.pfx", "wwwroot/js/evil.min.js"]) + unknown
        _, skipped = filter_files(split_files(diff))
        reasons = {s["path"]: s["reason"] for s in skipped}
        self.assertEqual(reasons, {"logo.png": "image, font or document", "fonts/a.woff2": "image, font or document",
                                   "app.js.map": "source map", "lib/x.dll": "executable or archive",
                                   "tools/y.jar": "executable or archive", "keys/app.pfx": "key or certificate",
                                   "wwwroot/js/evil.min.js": "minified", "data.bin": "binary of an unknown type"})
        self.assertEqual([s["path"] for s in unsafe_skips(skipped)],
                         ["lib/x.dll", "tools/y.jar", "keys/app.pfx", "wwwroot/js/evil.min.js", "data.bin"])

    def test_a_lockfile_is_safe_only_with_its_manifest_and_the_usual_registries(self):
        def lock(path, *lines):
            return git_file(path) + "".join(f"+{line}\n" for line in lines)
        npm = '"resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",'
        evil = '"resolved": "https://left-pad.example.net/left-pad-1.3.0.tgz",'
        funding = '"url": "https://opencollective.com/left-pad"'  # a funding link, not a download
        cases = {
            "lockfile": [lock("package-lock.json", npm, funding), git_file("package.json")],
            "lockfile changed without package.json": [lock("package-lock.json", npm)],
            "lockfile downloads from left-pad.example.net": [lock("package-lock.json", evil), git_file("package.json")],
            "lockfile ": [lock("packages.lock.json"), git_file("src/App/App.csproj")],  # a manifest by suffix
            "lockfile downloads from pkgs.example.org": [
                lock("uv.lock", 'url = "https://pkgs.example.org/simple/x-1.0.tar.gz"'), git_file("pyproject.toml")],
        }
        for reason, files in cases.items():
            with self.subTest(reason):
                _, skipped = filter_files(split_files("".join(files)))
                self.assertEqual(skipped[0]["reason"], reason.strip())
        with mock.patch.object(config, "TRUSTED_PACKAGE_HOSTS", {"example.org"}):
            _, skipped = filter_files(split_files("".join(cases["lockfile downloads from pkgs.example.org"])))
            self.assertEqual(skipped[0]["reason"], "lockfile")

    def test_a_lockfile_pointing_at_a_fork_or_another_format_is_caught(self):
        def reason(lockfile, manifest, *lines):
            diff = git_file(lockfile) + "".join(f"+{line}\n" for line in lines) + git_file(manifest)
            return filter_files(split_files(diff))[1][0]["reason"]
        cases = [  # (lockfile, manifest, added line, expected reason)
            ("Cargo.lock", "Cargo.toml", 'source = "registry+https://github.com/rust-lang/crates.io-index"', "lockfile"),
            ("Cargo.lock", "Cargo.toml", 'source = "registry+https://github.com/rust-lang/crates.io-index-x"',
             "lockfile downloads from github.com"),
            ("Cargo.lock", "Cargo.toml", 'source = "git+https://github.com/someone/fork#abc"',
             "lockfile downloads from github.com"),
            ("package-lock.json", "package.json",
             '"resolved": "https://codeload.github.com/someone/left-pad/tar.gz/abc123",',
             "lockfile downloads from codeload.github.com"),
            ("yarn.lock", "package.json", 'resolution: "left-pad@https://left-pad.example.net/x.tgz"',
             "lockfile downloads from left-pad.example.net"),
            ("mix.lock", "mix.exs", '"plug": {:git, "https://git.example.net/plug.git", "abc", []},',
             "lockfile downloads from git.example.net"),
            ("Podfile.lock", "Podfile", "    :git: https://git.example.net/Alamofire.git",
             "lockfile downloads from git.example.net"),
            ("Pipfile.lock", "Pipfile", '"url": "https://pkgs.example.net/simple",',
             "lockfile downloads from pkgs.example.net"),
            ("composer.lock", "composer.json", '"url": "https://repo.packagist.org/x.zip",',
             "lockfile in a format Vader can't check"),
        ]
        for lockfile, manifest, line, expected in cases:
            with self.subTest(line):
                self.assertEqual(reason(lockfile, manifest, line), expected)
        with mock.patch.object(config, "TRUSTED_PACKAGE_HOSTS", {"github.com"}):  # chosen knowingly
            self.assertEqual(reason("Cargo.lock", "Cargo.toml", cases[2][2]), "lockfile")

    def test_a_binary_lockfile_is_never_a_safe_skip(self):
        diff = ("diff --git a/bun.lockb b/bun.lockb\nBinary files a/bun.lockb and b/bun.lockb differ\n"
                + git_file("package.json"))
        _, skipped = filter_files(split_files(diff))
        self.assertEqual(skipped, [{"path": "bun.lockb", "reason": "lockfile in a format Vader can't check"}])
        self.assertEqual(unsafe_skips(skipped), skipped)

    def test_a_generator_banner_marks_a_file_generated(self):
        def new_file(path, first_line):
            return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
                    f"@@ -0,0 +1,3 @@\n+{first_line}\n+package x\n+func A() {{}}\n")
        banners = ["// Code generated by mockgen. DO NOT EDIT.", "// <auto-generated />", "# @generated by protoc",
                   "// GENERATED CODE - DO NOT MODIFY BY HAND", "# Generated by the protocol buffer compiler.  DO NOT EDIT!"]
        for banner in banners:
            with self.subTest(banner):
                self.assertTrue(has_generated_banner(banner))
                _, skipped = filter_files(split_files(new_file("x/mock.go", banner)))
                self.assertEqual(skipped, [{"path": "x/mock.go", "reason": "marked as generated"}])
        # Mentioning "generated" isn't a banner.
        self.assertFalse(has_generated_banner("# Builds the generated report. Edit freely."))
        kept, _ = filter_files(split_files(new_file("x/a.go", "// Package x generates reports.")))
        self.assertEqual(len(kept), 1)

    def test_a_hand_written_js_file_sharing_a_ts_name_is_still_reviewed(self):
        compiled = git_file("lib/index.js") + "+//# sourceMappingURL=index.js.map\n"
        diff = git_file("packages/a/src/index.ts") + git_file("tools/index.js") + compiled
        kept, skipped = filter_files(split_files(diff))
        self.assertEqual([f.path for f in kept], ["packages/a/src/index.ts", "tools/index.js"])
        self.assertEqual(skipped, [{"path": "lib/index.js", "reason": "compiled from TypeScript"}])


class NeedsPersonTests(unittest.TestCase):
    def test_path_patterns(self):
        paths = ["src/A.Data/DbContext.cs", "src/A.Data/Migrations/1_Init.cs", "src/A.Database/X.cs",
                 "src/A.Web/Program.cs", "tools/Program.cs", "src/A.Web/Services/Programs.cs"]
        self.assertEqual(needs_person(paths, ["src/A.Data/**"]), paths[:2])
        self.assertEqual(needs_person(paths, ["src/A.Data/"]), paths[:2])  # a folder means all under it
        self.assertEqual(needs_person(paths, ["src/A.Web/Program.cs"]), [paths[3]])
        self.assertEqual(needs_person(paths, ["Program.cs"]), [paths[3], paths[4]])  # any folder
        self.assertEqual(needs_person(paths, ["src/*/Program.cs"]), [paths[3]])  # `*` stays in one folder
        self.assertEqual(needs_person(paths, []), [])


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


if __name__ == "__main__":
    unittest.main()
