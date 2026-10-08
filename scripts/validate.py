"""The store's bot, without dashAI: checks what can be checked from GitHub and
the plugin's files alone, and builds index.json.

    python scripts/validate.py check <plugins/x.json>...   # a PR (PR_AUTHOR)
    python scripts/validate.py build                       # index.json, readmes/

Checked here: the entry, the repo (public, not archived, license), its
releases (one .zip/.tar.gz or the tag's source code, at most 200 MB, the
attestation with `gh attestation verify`) and the manifest (fields, id, the
version equals the tag, component types, each class exists, read with ast).
Not checked: what needs dashAI (name clashes with its components, pip
resolving the dependencies with its own, loading the code). dashAI checks all
of it again when installing: this only decides what the store lists.

Standard library and the `gh` CLI only; GITHUB_TOKEN raises GitHub's limit.
"""

import ast
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com/"
TOKEN = os.environ.get("GITHUB_TOKEN")
MAX_BYTES = 200 * 1024 * 1024  # dashAI's cap (sources/base.check_size)
KEEP = 5  # releases per plugin in the index
ID = re.compile(r"^(?!(con|prn|aux|nul|com\d|lpt\d)$)[a-z0-9]+(-[a-z0-9]+)*$")
REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
CLASS_REF = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
VERSION = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")
TYPES = {
    "Model", "Converter", "Metric", "Task", "DataLoader", "Explorer",
    "GlobalExplainer", "LocalExplainer", "Optimizer", "GenerativeModel",
    "GenerativeTask",
}  # dashAI's ALLOWED_COMPONENT_TYPES (plugins/validator.py)
LICENSES = {
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "GPL-3.0",
    "LGPL-3.0", "Unlicense",
}
ROOT = Path(".")


class Invalid(Exception):
    """What the store refuses; its message says why."""


def get(url):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dashai-plugins"}
    if TOKEN and url.startswith(API):  # the token only goes to the API
        headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers)) as r:
            data = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise Invalid(f"not found: {url}") from error
        raise
    if len(data) > MAX_BYTES:
        raise Invalid(f"larger than 200 MB: {url}")
    return data


def api(path):
    data = get(API + path)
    return json.loads(data) if data else None


def load_entry(path, removed):
    """The entry's own rules (shape, name, id); returns it."""
    path = Path(path)
    try:
        entry = json.loads(path.read_text("utf-8"))
    except ValueError as error:
        raise Invalid(f"{path.name} is not JSON: {error}") from error
    if not isinstance(entry, dict) or set(entry) != {"id", "repo"}:
        raise Invalid(f'{path.name} must have exactly "id" and "repo"')
    plugin_id, repo = entry["id"], entry["repo"]
    if path.name != f"{plugin_id}.json":
        raise Invalid(f'the file must be named after its id: plugins/{plugin_id}.json')
    if not isinstance(plugin_id, str) or not ID.match(plugin_id):
        raise Invalid(f"invalid id {plugin_id!r}: lowercase letters and digits, joined by single -")
    if "dashai" in plugin_id:
        raise Invalid('the id cannot contain "dashai"')
    if plugin_id in removed:
        raise Invalid(f"the id {plugin_id!r} belongs to a removed plugin")
    if not isinstance(repo, str) or not REPO.match(repo):
        raise Invalid(f'repo must be "owner/name", not {repo!r}')
    return entry


def check_repo(repo):
    try:
        data = api(f"repos/{repo}")
    except Invalid as error:
        raise Invalid(f"{repo} does not exist or is private") from error
    if data["full_name"].lower() != repo.lower():  # renamed or moved
        raise Invalid(f"{repo} is now {data['full_name']}: update the entry")
    if data["private"]:
        raise Invalid(f"{repo} is private")
    if data["archived"]:
        raise Invalid(f"{repo} is archived")
    spdx = (data.get("license") or {}).get("spdx_id")
    if spdx not in LICENSES:
        raise Invalid(f"{repo} has no allowed license (found {spdx}); allowed: {', '.join(sorted(LICENSES))}")


def releases(repo):
    found = api(f"repos/{repo}/releases?per_page=20")
    found = [r for r in found if not r["draft"] and not r["prerelease"]]
    if not found:
        raise Invalid(f"{repo} has no published release")
    return found[:KEEP]


def extract(blob, name, into):
    """A .zip or .tar.gz into ``into``; returns the folder with manifest.json
    (the archive's root or its only folder, as dashAI looks for it)."""
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                if sum(i.file_size for i in archive.infolist()) > MAX_BYTES:
                    raise Invalid(f"{name}: larger than 200 MB extracted")
                archive.extractall(into)  # drops absolute and .. paths
        else:
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
                if sum(m.size for m in archive.getmembers()) > MAX_BYTES:
                    raise Invalid(f"{name}: larger than 200 MB extracted")
                archive.extractall(into, filter="data")
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as error:
        raise Invalid(f"{name} cannot be extracted: {error}") from error
    if (into / "manifest.json").is_file():
        return into
    folders = [p for p in into.iterdir() if p.is_dir() and not p.name.startswith((".", "__MACOSX"))]
    if len(folders) == 1 and (folders[0] / "manifest.json").is_file():
        return folders[0]
    raise Invalid(f"{name} has no manifest.json at its root")


def fetch(repo, release, into):
    """What dashAI would download for this release: (folder, sha256, level).
    The sha256 is computed as dashAI does, so installing can compare it."""
    tag = release["tag_name"]
    assets = [a for a in release["assets"] if a["name"].lower().endswith((".zip", ".tar.gz"))]
    if len(assets) == 1:
        asset = assets[0]
        if asset["size"] > MAX_BYTES:
            raise Invalid(f"{asset['name']} is larger than 200 MB")
        blob = get(asset["browser_download_url"])
        sha256 = hashlib.sha256(blob).hexdigest()
        if asset.get("digest") and asset["digest"] != f"sha256:{sha256}":
            raise Invalid(f"{asset['name']} does not match GitHub's digest")
        file = into / asset["name"]
        file.write_bytes(blob)
        attested = subprocess.run(
            ["gh", "attestation", "verify", str(file), "--repo", repo,
             "--source-ref", f"refs/tags/{tag}"],
            capture_output=True,
        ).returncode == 0
        folder = extract(blob, asset["name"].lower(), into / "plugin")
        return folder, sha256, "github_attested" if attested else "github_asset"
    # No single file: the source code at the tag's commit (a tag can move).
    commit = api(f"repos/{repo}/commits/{urllib.parse.quote(tag, safe='')}")["sha"]
    blob = get(f"https://codeload.github.com/{repo}/tar.gz/{commit}")
    sha256 = hashlib.sha256(gzip.decompress(blob)).hexdigest()  # the tar's, as dashAI
    return extract(blob, "source.tar.gz", into / "plugin"), sha256, "github_source"


def defines(folder, class_ref):
    """Whether ``module:Class`` names a class defined in the plugin's files."""
    module, name = class_ref.split(":")
    base = folder.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            try:
                tree = ast.parse(path.read_bytes())
            except SyntaxError as error:
                raise Invalid(f"{path.name} has a syntax error: {error}") from error
            return any(isinstance(n, ast.ClassDef) and n.name == name for n in tree.body)
    return False


def http_link(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def check_manifest(folder, plugin_id, tag):
    try:
        manifest = json.loads((folder / "manifest.json").read_text("utf-8-sig"))
    except ValueError as error:
        raise Invalid(f"manifest.json is not UTF-8 JSON: {error}") from error
    if manifest.get("manifest_version") != "1":
        raise Invalid('manifest_version must be "1"')
    if manifest.get("id") != plugin_id:
        raise Invalid(f"the manifest's id is {manifest.get('id')!r}, the entry's {plugin_id!r}")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION.match(version):
        raise Invalid(f"invalid version {version!r}")
    if version != tag.removeprefix("v"):
        raise Invalid(f"version {version} does not match the tag {tag}")
    if not isinstance(manifest.get("min_app_version"), str) or not VERSION.match(manifest["min_app_version"]):
        raise Invalid("min_app_version must be a version, e.g. 0.10.0")
    if not str(manifest.get("name") or "").strip():
        raise Invalid("the manifest needs a name")
    authors = manifest.get("authors")
    if not isinstance(authors, list) or not authors:
        raise Invalid("the manifest needs at least one author")
    links = [a.get("url") for a in authors if isinstance(a, dict) and a.get("url")]
    links += list((manifest.get("project_urls") or {}).values())
    if not all(http_link(link) for link in links):
        raise Invalid("every link must be http(s)")
    components = manifest.get("components")
    if not isinstance(components, list) or not components:
        raise Invalid("the manifest needs at least one component")
    for component in components:
        ref, kind = component.get("class", ""), component.get("type")
        if not CLASS_REF.match(ref):
            raise Invalid(f"class must be 'module:Class', not {ref!r}")
        if kind not in TYPES:
            raise Invalid(f"{ref}: type {kind!r} is not one dashAI accepts")
        if not defines(folder, ref):
            raise Invalid(f"{ref}: no such class in the plugin")
    names = [c["class"].split(":")[1] for c in components]
    if len(set(names)) != len(names):
        raise Invalid("component class names must be unique")
    dependencies = manifest.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
        raise Invalid("dependencies must be a list of pip requirements")
    return manifest


def describe(entry, log):
    """The entry as index.json lists it, with its README; None if no release
    passes. ``log`` gets one line per release."""
    repo = entry["repo"]
    check_repo(repo)
    passed = []
    for release in releases(repo):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                folder, sha256, level = fetch(repo, release, Path(tmp))
                manifest = check_manifest(folder, entry["id"], release["tag_name"])
            except Invalid as error:
                log(f"  {release['tag_name']}: {error}")
                continue
            readme = folder / "README.md"
            readme = readme.read_text("utf-8", errors="replace") if readme.is_file() else None
        log(f"  {release['tag_name']}: ok ({level})")
        passed.append((release, manifest, sha256, level, readme))
    if not passed:
        return None, None
    _, latest, _, _, readme = passed[0]
    components = latest["components"]
    return {
        "id": entry["id"],
        "source": "git",
        "location": f"https://github.com/{repo}",
        "name": latest["name"],
        "description": latest.get("description", ""),
        "authors": [
            {key: str(author.get(key, "")) for key in ("name", "email", "url")}
            for author in latest["authors"]
        ],
        "components": [c["class"].split(":")[1] for c in components],
        "component_types": [c["type"] for c in components],
        "releases": [
            {
                "tag": release["tag_name"],
                "version": manifest["version"],
                "min_app_version": manifest["min_app_version"],
                "trust_level": level,
                "sha256": sha256,
                "published_at": release["published_at"],
                "dependencies": manifest.get("dependencies", []),
            }
            for release, manifest, sha256, level, _ in passed
        ],
        "project_urls": latest.get("project_urls", {}),
    }, readme


def removed_ids():
    return {item["id"] for item in json.loads((ROOT / "removed.json").read_text("utf-8"))}


def entries():
    return sorted((ROOT / "plugins").glob("*.json"))


def check(paths):
    """A PR: only entries change, each passes, owned by the PR's author."""
    author = os.environ["PR_AUTHOR"].lower()
    report, failed = [], False
    others = json.loads((ROOT / "index.json").read_text("utf-8"))["plugins"]
    for path in paths:
        lines = []
        try:
            entry = load_entry(path, removed_ids())
            owner = entry["repo"].split("/")[0].lower()
            if owner != author:
                try:  # an organization's public member may publish its repos
                    api(f"orgs/{owner}/public_members/{author}")
                except Invalid as error:
                    raise Invalid(f"only {owner} (or a public member of it) can add this repo") from error
            repos = [json.loads(p.read_text("utf-8"))["repo"].lower() for p in entries() if p.name != Path(path).name]
            if entry["repo"].lower() in repos:
                raise Invalid(f"{entry['repo']} is already in the store")
            listed, _ = describe(entry, lines.append)
            if listed is None:
                raise Invalid("no release passes")
            taken = {c: p["id"] for p in others if p["id"] != entry["id"] for c in p["components"]}
            clash = [c for c in listed["components"] if c in taken]
            if clash:
                raise Invalid(f"components already in the store: {', '.join(f'{c} ({taken[c]})' for c in clash)}")
            latest = listed["releases"][0]
            report.append(f"✅ **{entry['id']}** {latest['tag']} ({latest['trust_level']}): {', '.join(listed['components'])}")
        except Invalid as error:
            failed = True
            report.append(f"❌ **{Path(path).name}**: {error}")
        report += lines
    print("\n".join(report))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as file:
            file.write("\n\n".join(report) + "\n")
    return 1 if failed else 0


def build():
    """index.json and readmes/ from every entry. A plugin that can't be read
    now (GitHub down, rate limit) keeps what the index had."""
    path = ROOT / "index.json"
    old = json.loads(path.read_text("utf-8")) if path.is_file() else {"plugins": []}
    kept = {p["id"]: p for p in old["plugins"]}
    removed = json.loads((ROOT / "removed.json").read_text("utf-8"))
    plugins, readmes = [], {}
    for entry_path in entries():
        print(entry_path.name)
        try:
            entry = load_entry(entry_path, {item["id"] for item in removed})
            listed, readme = describe(entry, print)
        except Invalid as error:
            print(f"  left out: {error}")
            continue
        except (urllib.error.URLError, OSError) as error:
            print(f"  kept as it was: {error}")
            if entry_path.stem in kept:
                plugins.append(kept[entry_path.stem])
            continue
        if listed is None:
            print("  left out: no release passes")
            continue
        plugins.append(listed)
        if readme:
            readmes[listed["id"]] = readme
    if plugins == old["plugins"] and removed == old.get("removed"):
        print("index.json: no changes")
        return 0
    index = {
        "schema": 1,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plugins": plugins,
        "removed": removed,
    }
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", "utf-8", newline="\n")
    folder = ROOT / "readmes"
    folder.mkdir(exist_ok=True)
    listed_ids = {p["id"] for p in plugins}
    for stale in folder.glob("*.md"):
        if stale.stem not in listed_ids:
            stale.unlink()
    for plugin_id, text in readmes.items():
        (folder / f"{plugin_id}.md").write_text(text, "utf-8", newline="\n")
    print(f"index.json: {len(plugins)} plugins")
    return 0


if __name__ == "__main__":
    command, *args = sys.argv[1:] or ["build"]
    sys.exit(check(args) if command == "check" else build())
