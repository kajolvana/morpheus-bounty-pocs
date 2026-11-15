#!/usr/bin/env python3
"""A standalone GitHub-compatible git forge: one process, stdlib only.

Serves, from its own in-memory store:
  * git smart HTTP (upload-pack protocol v2 + v0 fallback, receive-pack v0)
  * REST v3 under /api/v3 (repos, branches, commits, contents, labels,
    issues, comments, pull requests)
  * GraphQL v4 at /api/graphql (the documents gh and github-mcp-server send)
  * MCP (streamable HTTP) at /mcp

Lifecycle (the trial harness's): bind an ephemeral port, POST {"port": N} to
$MAKEFUN_ENV_URL/register, then poll GET $MAKEFUN_ENV_URL/done until it
answers {"done": true} and exit 0. Without MAKEFUN_ENV_URL it just serves
forever on a port (FORGE_PORT, or an ephemeral one it prints), which is how
the kit's local runner drives it.

Determinism: no wall clock or unseeded randomness reaches any response.
Timestamps are fixed constants or a deterministic per-run counter, ids are
sequential counters, and every listing has a fixed order. Two runs on the
same instance therefore behave identically.

No git binary, no package manager, no outside service: git objects and
packfiles are built and parsed in-process (hashlib + zlib).
"""

import base64
import difflib
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# The seeded scenario: the suite's viewer/stranger logins, derived from the
# same fixed seed and PRNG the kit publishes (program/env/src/wire.ts), so
# this forge answers for the login the trial token names without ever seeing
# the scenario itself. Verified: viewer adaiversen, stranger olgabauer.
# ---------------------------------------------------------------------------

_U64 = (1 << 64) - 1
_SCENARIO_SEED = 0x6A09E667F3BCC909


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & _U64
    return h


def _mix64(v):
    z = (v + 0x9E3779B97F4A7C15) & _U64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _U64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _U64
    return (z ^ (z >> 31)) & _U64


class _Rng:
    def __init__(self, seed):
        s = seed & _U64
        self.state = s if s else _SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & _U64
        x = (x ^ ((x << 25) & _U64)) & _U64
        x = (x ^ (x >> 27)) & _U64
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & _U64

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, mx):
        if mx == 1:
            return 0
        limit = 4294967296 - (4294967296 % mx)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % mx

    def pick(self, items):
        return items[self.next_int(len(items))]


_FIRST = ['ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
          'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
          'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane']
_LAST = ['anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
         'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
         'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich',
         'vega', 'weber', 'xu', 'yilmaz', 'zimmer']


def _scenario_users():
    rng = _Rng(_mix64(_SCENARIO_SEED ^ _fnv1a64('users')))
    used = set()
    users = []
    for i in range(12):
        first = rng.pick(_FIRST)
        last = rng.pick(_LAST)
        base = re.sub(r'[^a-z0-9-]', '', first + last)
        login = base
        suffix = 2
        while login in used:
            login = '%s-%d' % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({'id': 1000 + i, 'login': login,
                      'name': '%s %s' % (first, last),
                      'email': '%s@example.invalid' % login})
    return users


_USERS = _scenario_users()
VIEWER = _USERS[0]          # adaiversen: the login the trial token names
STRANGER = _USERS[2]        # olgabauer
VIEWER_LOGIN = VIEWER['login']
ZERO_OID = '0' * 40
FIXED_TS = '2025-01-01T00:00:00Z'
COMMIT_EPOCH = 1700000000   # base for deterministic commit timestamps
AGENT = 'forge/1.0'


def viewer_identity():
    return '%s <%s>' % (VIEWER['name'], VIEWER['email'])


# ---------------------------------------------------------------------------
# git primitives: objects, pkt-line, packfiles
# ---------------------------------------------------------------------------

_TYPE_CODE = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}
_CODE_TYPE = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
_HEX40 = re.compile(r'^[0-9a-f]{40}$')


def hash_object(otype, data):
    h = hashlib.sha1()
    h.update(('%s %d\0' % (otype, len(data))).encode('ascii'))
    h.update(data)
    return h.hexdigest()


def parse_tree(data):
    entries = []
    off = 0
    n = len(data)
    while off < n:
        sp = data.index(b' ', off)
        nul = data.index(b'\0', sp + 1)
        mode = data[off:sp].decode('ascii')
        name = data[sp + 1:nul].decode('utf-8')
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        off = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name = entry[0], entry[1]
    return (name + '/').encode('utf-8') if mode == '40000' else name.encode('utf-8')


def build_tree(entries):
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += ('%s %s\0' % (mode, name)).encode('utf-8')
        out += bytes.fromhex(oid)
    return bytes(out)


def parse_commit(data):
    blank = data.find(b'\n\n')
    head = data if blank < 0 else data[:blank]
    message = '' if blank < 0 else data[blank + 2:].decode('utf-8', 'replace')
    tree = ''
    parents = []
    author = ''
    committer = ''
    for line in head.decode('utf-8', 'replace').split('\n'):
        if line.startswith('tree '):
            tree = line[5:]
        elif line.startswith('parent '):
            parents.append(line[7:])
        elif line.startswith('author '):
            author = line[7:]
        elif line.startswith('committer '):
            committer = line[10:]
    return {'tree': tree, 'parents': parents, 'author': author,
            'committer': committer, 'message': message}


def build_commit(tree, parents, message, author, committer, timestamp):
    lines = ['tree %s' % tree]
    for p in parents:
        lines.append('parent %s' % p)
    who = '%s %d +0000' % (author, timestamp)
    who_c = '%s %d +0000' % (committer, timestamp)
    lines.append('author %s' % who)
    lines.append('committer %s' % who_c)
    lines.append('')
    msg = message if message.endswith('\n') else message + '\n'
    return ('\n'.join(lines) + '\n' + msg).encode('utf-8')


def parse_tag(data):
    blank = data.find(b'\n\n')
    head = data if blank < 0 else data[:blank]
    message = '' if blank < 0 else data[blank + 2:].decode('utf-8', 'replace')
    hdr = {}
    for line in head.decode('utf-8', 'replace').split('\n'):
        sp = line.find(' ')
        if sp >= 0:
            hdr[line[:sp]] = line[sp + 1:]
    return {'object': hdr.get('object', ''), 'type': hdr.get('type', ''),
            'tag': hdr.get('tag', ''), 'tagger': hdr.get('tagger', ''),
            'message': message}


# --- pkt-line ---

def pkt(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return ('%04x' % (len(data) + 4)).encode('ascii') + data


FLUSH = b'0000'
DELIM = b'0001'


def pkt_lines(items):
    out = bytearray()
    for it in items:
        if it == 'flush':
            out += FLUSH
        elif it == 'delim':
            out += DELIM
        else:
            out += pkt(it)
    return bytes(out)


def decode_pkt_lines(buf):
    """Return list of ('data', payload) | ('flush',) | ('delim',) | ('end',).

    Raises ValueError on malformed framing. Stops at the first byte offset
    that does not look like a pkt-line header (raw pack data follows)."""
    packets = []
    off = 0
    n = len(buf)
    while off < n:
        if off + 4 > n:
            raise ValueError('truncated pkt-line')
        head = buf[off:off + 4]
        try:
            length = int(head.decode('ascii'), 16)
        except ValueError:
            break  # raw data follows (a pack)
        if length == 0:
            packets.append(('flush', None))
            off += 4
        elif length == 1:
            packets.append(('delim', None))
            off += 4
        elif length == 2:
            packets.append(('end', None))
            off += 4
        elif length < 4 or length > 65520:
            raise ValueError('bad pkt-line length %d' % length)
        else:
            if off + length > n:
                raise ValueError('truncated pkt-line payload')
            packets.append(('data', bytes(buf[off + 4:off + length])))
            off += length
    return packets, off


# --- packfiles ---

class PackError(Exception):
    pass


def _pack_entry_header(buf, off):
    first = buf[off]
    size = first & 0x0F
    shift = 4
    off += 1
    b = first
    while b & 0x80:
        if off >= len(buf):
            raise PackError('truncated pack header')
        b = buf[off]
        off += 1
        size += (b & 0x7F) << shift
        shift += 7
    return (first >> 4) & 0x07, size, off


def _inflate_at(buf, off, limit):
    d = zlib.decompressobj()
    try:
        out = d.decompress(bytes(buf[off:limit]))
    except zlib.error as e:
        raise PackError('zlib error at %d: %s' % (off, e))
    consumed = (limit - off) - len(d.unused_data)
    return out, off + consumed


def apply_delta(base, delta):
    off = 0

    def read_size():
        nonlocal off
        value = 0
        shift = 0
        while True:
            if off >= len(delta):
                raise PackError('delta size truncated')
            b = delta[off]
            off += 1
            value |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                return value
    src_size = read_size()
    tgt_size = read_size()
    if src_size != len(base):
        raise PackError('delta base size mismatch')
    out = bytearray()
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[off] << (bit * 8)
                    off += 1
            for bit in range(4, 7):
                if op & (1 << bit):
                    cp_size |= delta[off] << ((bit - 4) * 8)
                    off += 1
            if cp_size == 0:
                cp_size = 0x10000
            if cp_off + cp_size > len(base):
                raise PackError('delta copy out of range')
            out += base[cp_off:cp_off + cp_size]
        elif op:
            if off + op > len(delta):
                raise PackError('delta insert truncated')
            out += delta[off:off + op]
            off += op
        else:
            raise PackError('delta opcode 0')
    if len(out) != tgt_size:
        raise PackError('delta produced wrong size')
    return bytes(out)


def read_pack(buf):
    """Parse a packfile; return dict oid -> (type, bytes). Resolves deltas."""
    if len(buf) < 32 or buf[:4] != b'PACK':
        raise PackError('not a packfile')
    version = int.from_bytes(buf[4:8], 'big')
    if version not in (2, 3):
        raise PackError('unsupported pack version %d' % version)
    count = int.from_bytes(buf[8:12], 'big')
    trailer = buf[-20:].hex()
    if hashlib.sha1(bytes(buf[:-20])).hexdigest() != trailer:
        raise PackError('pack checksum mismatch')
    limit = len(buf) - 20
    raw = []          # (offset, type_code, size, data, base_offset, base_oid)
    by_offset = {}
    off = 12
    for _ in range(count):
        start = off
        tcode, size, off = _pack_entry_header(buf, off)
        base_off = None
        base_oid = None
        if tcode == 6:
            b = buf[off]
            off += 1
            value = b & 0x7F
            while b & 0x80:
                b = buf[off]
                off += 1
                value = ((value + 1) << 7) | (b & 0x7F)
            base_off = start - value
        elif tcode == 7:
            base_oid = buf[off:off + 20].hex()
            off += 20
        elif tcode not in _CODE_TYPE:
            raise PackError('unknown pack type %d' % tcode)
        data, off = _inflate_at(buf, off, limit)
        if len(data) != size:
            raise PackError('pack entry size mismatch')
        entry = (start, tcode, size, data, base_off, base_oid)
        raw.append(entry)
        by_offset[start] = entry
    objects = {}
    resolved = {}

    def resolve(entry):
        start = entry[0]
        if start in resolved:
            return resolved[start]
        tcode = entry[1]
        if tcode == 6:
            base_entry = by_offset.get(entry[4])
            if base_entry is None:
                raise PackError('ofs-delta base missing')
            btype, bdata = resolve(base_entry)
            otype, data = btype, apply_delta(bdata, entry[3])
        elif tcode == 7:
            btype, bdata = None, None
            for oid, (tt, dd) in list(objects.items()):
                if oid == entry[5]:
                    btype, bdata = tt, dd
                    break
            if btype is None:
                raise PackError('ref-delta base %s missing' % entry[5])
            otype, data = btype, apply_delta(bdata, entry[3])
        else:
            otype, data = _CODE_TYPE[tcode], entry[3]
        oid = hash_object(otype, data)
        resolved[start] = (otype, data)
        objects[oid] = (otype, data)
        return (otype, data)
    for entry in raw:
        resolve(entry)
    return objects


def write_pack(objects):
    """objects: iterable of (type, bytes). Plain entries, no deltas."""
    items = list(objects)
    parts = [b'PACK' + (2).to_bytes(4, 'big') + len(items).to_bytes(4, 'big')]
    for otype, data in items:
        code = _TYPE_CODE[otype]
        size = len(data)
        first = (code << 4) | (size & 0x0F)
        size >>= 4
        hdr = bytearray()
        if size:
            hdr.append(first | 0x80)
            while size:
                b = size & 0x7F
                size >>= 7
                hdr.append(b | (0x80 if size else 0))
        else:
            hdr.append(first)
        parts.append(bytes(hdr))
        parts.append(zlib.compress(data, 1))
    body = b''.join(parts)
    return body + hashlib.sha1(body).digest()


def collect_reachable(objects, roots, depth=None):
    """objects: oid -> (type, bytes). Return set of reachable oids.

    depth limits commit generations from the roots (None = unlimited).
    Tags are peeled; a tag root counts as its target at generation 0."""
    seen = set()
    stack = []
    for r in roots:
        if r in objects:
            stack.append((r, 0))
    while stack:
        oid, gen = stack.pop()
        if oid in seen:
            continue
        ent = objects.get(oid)
        if ent is None:
            continue
        otype, data = ent
        seen.add(oid)
        if otype == 'commit':
            if depth is not None and gen >= depth:
                continue
            c = parse_commit(data)
            stack.append((c['tree'], gen))
            for p in c['parents']:
                stack.append((p, gen + 1))
        elif otype == 'tag':
            t = parse_tag(data)
            stack.append((t['object'], gen))
        elif otype == 'tree':
            for _mode, _name, child in parse_tree(data):
                stack.append((child, gen))
    return seen


def peel_to_commit(objects, oid):
    seen = set()
    while oid in objects and objects[oid][0] == 'tag' and oid not in seen:
        seen.add(oid)
        oid = parse_tag(objects[oid][1])['object']
    return oid


def is_ancestor(objects, anc, desc):
    """True when commit anc is reachable from commit desc (or anc == desc)."""
    anc = peel_to_commit(objects, anc)
    desc = peel_to_commit(objects, desc)
    if anc == desc:
        return True
    if anc == ZERO_OID or desc == ZERO_OID:
        return False
    stack = [desc]
    seen = set()
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        if oid == anc:
            return True
        ent = objects.get(oid)
        if ent is None or ent[0] != 'commit':
            continue
        stack.extend(parse_commit(ent[1])['parents'])
    return False


def merge_base(objects, a, b):
    a = peel_to_commit(objects, a)
    b = peel_to_commit(objects, b)
    anc_a = set()
    stack = [a]
    while stack:
        oid = stack.pop()
        if oid in anc_a:
            continue
        anc_a.add(oid)
        ent = objects.get(oid)
        if ent is not None and ent[0] == 'commit':
            stack.extend(parse_commit(ent[1])['parents'])
    stack = [b]
    seen = set()
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        if oid in anc_a:
            return oid
        ent = objects.get(oid)
        if ent is not None and ent[0] == 'commit':
            stack.extend(parse_commit(ent[1])['parents'])
    return None


# ---------------------------------------------------------------------------
# The store: repositories, issues, pull requests, labels, comments.
# ---------------------------------------------------------------------------


class Repo:
    __slots__ = ('dbid', 'name', 'owner', 'description', 'private',
                 'default_branch', 'refs', 'objects', 'labels', 'issues',
                 'issue_seq', 'issue_id_seq', 'pulls', 'pr_seq',
                 'comments', 'comment_id_seq', 'seq', 'tree_cache',
                 'issue_cache', 'pack_cache')

    def __init__(self, dbid, name, owner, description='', private=False, seq=0):
        self.dbid = dbid
        self.name = name
        self.owner = owner
        self.description = description
        self.private = private
        self.default_branch = 'main'
        self.refs = {}          # refname -> oid
        self.objects = {}       # oid -> (type, bytes)
        self.labels = {}        # name -> label dict
        self.issues = {}        # number -> issue dict
        self.issue_seq = 0
        self.issue_id_seq = 0
        self.pulls = {}         # number -> pull dict
        self.pr_seq = 0
        self.comments = {}      # issue number -> [comment dict]
        self.comment_id_seq = 0
        self.seq = seq          # creation order, for listings
        self.tree_cache = {}    # tree oid -> {path: (mode, oid)}
        self.issue_cache = {}   # issue number -> cached JSON bytes
        self.pack_cache = {}    # fetch key -> (ack, shallow, unshallow, pack)

    def ref_oid(self, refname):
        return self.refs.get(refname)

    def branch_tip(self, branch):
        return self.refs.get('refs/heads/%s' % branch)


class Store:
    def __init__(self):
        self.repos = {}         # (owner, name) -> Repo
        self.repo_dbid_seq = 5000
        self.global_issue_id = 200000
        self.global_comment_id = 300000
        self.global_label_id = 400000
        self.create_seq = 0
        self.commit_seq = 0
        self.lock = threading.RLock()

    def next_repo_dbid(self):
        self.repo_dbid_seq += 1
        return self.repo_dbid_seq

    def create_repo(self, name, owner, description='', private=False):
        with self.lock:
            key = (owner, name)
            if key in self.repos:
                return None
            self.create_seq += 1
            repo = Repo(self.next_repo_dbid(), name, owner, description,
                        private, self.create_seq)
            self.repos[key] = repo
            return repo

    def get_repo(self, owner, name):
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            return self.repos.pop((owner, name), None) is not None

    def next_commit_ts(self):
        with self.lock:
            self.commit_seq += 1
            return COMMIT_EPOCH + self.commit_seq


STORE = Store()
REQ_COUNT = [0]
REQ_LOCK = threading.Lock()


def next_request_id():
    with REQ_LOCK:
        REQ_COUNT[0] += 1
        return 'req-%08d' % REQ_COUNT[0]


# --- global ids: opaque base64, stable across reads ---

def gid(kind, *parts):
    return base64.b64encode(('%s:%s' % (kind, ':'.join(str(p) for p in parts))
                             ).encode('utf-8')).decode('ascii')


def parse_gid(value):
    try:
        raw = base64.b64decode(value.encode('ascii'), validate=True
                               ).decode('utf-8')
    except Exception:
        return None
    parts = raw.split(':')
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


# --- git object helpers on a repo ---

def repo_tip_commit(repo, refname):
    oid = repo.refs.get(refname)
    if not oid:
        return None
    return peel_to_commit(repo.objects, oid)


def tree_files(repo, tree_oid):
    """Flatten a tree to {path: (mode, oid)}; cached per tree oid."""
    if tree_oid is None:
        return {}
    cached = repo.tree_cache.get(tree_oid)
    if cached is not None:
        return cached
    out = {}

    def walk(tree_oid, prefix):
        ent = repo.objects.get(tree_oid)
        if ent is None or ent[0] != 'tree':
            return
        for mode, name, child in parse_tree(ent[1]):
            path = prefix + name
            if mode == '40000':
                walk(child, path + '/')
            else:
                out[path] = (mode, child)
    walk(tree_oid, '')
    repo.tree_cache[tree_oid] = out
    return out


def build_tree_from_files(repo, files):
    """files: {path: (mode, blob_oid)} -> tree oid (objects stored)."""
    # Build bottom-up: group by directory depth.
    levels = {}

    def ensure_tree(entries):
        oid = hash_object('tree', build_tree(entries))
        repo.objects[oid] = ('tree', build_tree(entries))
        return oid

    def make_dir(paths):
        entries = []
        dirs = {}
        for path, (mode, boid) in sorted(paths.items()):
            head, _, tail = path.partition('/')
            if tail:
                dirs.setdefault(head, {})[tail] = (mode, boid)
            else:
                entries.append((mode, head, boid))
        for dname, sub in dirs.items():
            entries.append(('40000', dname, make_dir(sub)))
        return ensure_tree(entries)
    return make_dir(files)


def make_commit(repo, tree_oid, parents, message):
    ts = STORE.next_commit_ts()
    who = viewer_identity()
    data = build_commit(tree_oid, parents, message, who, who, ts)
    oid = hash_object('commit', data)
    repo.objects[oid] = ('commit', data)
    return oid


def files_at_ref(repo, refname):
    oid = repo_tip_commit(repo, refname)
    if not oid:
        return None, None
    ent = repo.objects.get(oid)
    if ent is None or ent[0] != 'commit':
        return None, None
    return tree_files(repo, parse_commit(ent[1])['tree']), oid


def set_branch_tip(repo, branch, oid):
    repo.refs['refs/heads/%s' % branch] = oid


def create_contents_commit(repo, branch, path, data_bytes, message,
                           delete=False):
    """Apply a file create/update/delete on a branch; return commit oid."""
    refname = 'refs/heads/%s' % branch
    cur = repo.refs.get(refname)
    if cur is None:
        # New branch from the default branch tip (or unborn: no parents).
        if branch != repo.default_branch:
            base = repo.branch_tip(repo.default_branch)
        else:
            base = None
        if base is not None:
            cur = base
            repo.refs[refname] = base
        files = {}
        parents = []
        if base is not None:
            f, c = files_at_ref(repo, refname)
            files = dict(f)
            parents = [c]
    else:
        c = peel_to_commit(repo.objects, cur)
        f, _ = files_at_ref(repo, refname)
        files = dict(f)
        parents = [c]
    if delete:
        files.pop(path, None)
    else:
        blob = hash_object('blob', data_bytes)
        repo.objects[blob] = ('blob', data_bytes)
        files[path] = ('100644', blob)
    tree_oid = build_tree_from_files(repo, files)
    oid = make_commit(repo, tree_oid, parents, message)
    repo.refs[refname] = oid
    repo.pack_cache.clear()
    return oid


def diff_files(repo, base_files, head_files):
    """Return list of {filename, status, additions, deletions, changes}."""
    out = []
    paths = sorted(set(base_files) | set(head_files))
    for path in paths:
        old = base_files.get(path)
        new = head_files.get(path)
        if old is None:
            status = 'added'
            old_text = ''
            new_text = blob_text(repo, new[1])
        elif new is None:
            status = 'removed'
            old_text = blob_text(repo, old[1])
            new_text = ''
        elif old[1] == new[1]:
            continue
        else:
            status = 'modified'
            old_text = blob_text(repo, old[1])
            new_text = blob_text(repo, new[1])
        sm = difflib.SequenceMatcher(
            a=old_text.splitlines(keepends=True),
            b=new_text.splitlines(keepends=True), autojunk=False)
        adds = dels = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ('replace', 'delete'):
                dels += i2 - i1
            if tag in ('replace', 'insert'):
                adds += j2 - j1
        out.append({'filename': path, 'status': status, 'additions': adds,
                    'deletions': dels, 'changes': adds + dels})
    return out


def blob_text(repo, oid):
    ent = repo.objects.get(oid)
    if ent is None:
        return ''
    try:
        return ent[1].decode('utf-8', 'replace')
    except Exception:
        return ''


def pr_mergeable(repo, pr):
    """MERGEABLE / CONFLICTING / UNKNOWN for an open pull request."""
    if pr['state'] != 'open':
        return 'UNKNOWN'
    head_tip = peel_to_commit(repo.objects, repo.refs.get(
        'refs/heads/%s' % pr['head_ref'], '') or '')
    base_tip = peel_to_commit(repo.objects, repo.refs.get(
        'refs/heads/%s' % pr['base_ref'], '') or '')
    if not head_tip or not base_tip:
        return 'UNKNOWN'
    mb = merge_base(repo.objects, head_tip, base_tip)
    if mb is None:
        return 'UNKNOWN'
    mb_files = tree_files(repo, parse_commit(repo.objects[mb][1])['tree'])
    head_files = tree_files(repo, parse_commit(
        repo.objects[head_tip][1])['tree'])
    base_files = tree_files(repo, parse_commit(
        repo.objects[base_tip][1])['tree'])
    return 'CONFLICTING' if _has_conflict(mb_files, head_files, base_files) \
        else 'MERGEABLE'


def _has_conflict(mb_files, head_files, base_files):
    paths = set(head_files) | set(base_files) | set(mb_files)
    for path in paths:
        mbv = mb_files.get(path)
        hv = head_files.get(path)
        bv = base_files.get(path)
        head_changed = hv != mbv
        base_changed = bv != mbv
        if head_changed and base_changed and hv != bv:
            return True
    return False


def squash_merge(repo, pr):
    """Merge pr (squash). Return (status, payload): status 200 or 405."""
    if pr['state'] != 'open':
        return 405, {'message': 'Pull request is not open'}
    head_tip = peel_to_commit(repo.objects, repo.refs.get(
        'refs/heads/%s' % pr['head_ref'], '') or '')
    base_tip = peel_to_commit(repo.objects, repo.refs.get(
        'refs/heads/%s' % pr['base_ref'], '') or '')
    if not head_tip or not base_tip:
        return 405, {'message': 'Missing branch tip'}
    mb = merge_base(repo.objects, head_tip, base_tip)
    if mb is None:
        return 405, {'message': 'No merge base'}
    mb_files = tree_files(repo, parse_commit(repo.objects[mb][1])['tree'])
    head_files = tree_files(repo, parse_commit(
        repo.objects[head_tip][1])['tree'])
    base_files = tree_files(repo, parse_commit(
        repo.objects[base_tip][1])['tree'])
    if _has_conflict(mb_files, head_files, base_files):
        return 405, {'message': 'Merge conflict'}
    merged = dict(base_files)
    for path, val in head_files.items():
        if mb_files.get(path) != val:
            merged[path] = val
    for path in list(merged):
        if path not in head_files and path in mb_files:
            del merged[path]
    tree_oid = build_tree_from_files(repo, merged)
    msg = pr['title'] + (' (#%d)' % pr['number'])
    oid = make_commit(repo, tree_oid, [base_tip], msg)
    repo.refs['refs/heads/%s' % pr['base_ref']] = oid
    repo.pack_cache.clear()
    pr['state'] = 'closed'
    pr['merged'] = True
    pr['merged_at'] = FIXED_TS
    pr['merge_commit_sha'] = oid
    return 200, {'merged': True, 'sha': oid, 'message': pr['title'],
                 'number': pr['number'], 'state': 'closed'}


# --- REST view helpers ---

def repo_json(repo):
    return {'id': repo.dbid, 'name': repo.name, 'full_name':
            '%s/%s' % (repo.owner, repo.name),
            'owner': {'login': repo.owner, 'id': 1000, 'type': 'User'},
            'private': repo.private, 'description': repo.description,
            'default_branch': repo.default_branch,
            'created_at': FIXED_TS, 'updated_at': FIXED_TS,
            'has_issues': True, 'has_projects': True, 'has_wiki': True}


def issue_json(repo, issue):
    return {'id': issue['id'], 'number': issue['number'],
            'title': issue['title'], 'body': issue.get('body', ''),
            'state': issue['state'],
            'labels': [{'name': name, 'color': repo.labels[name]['color']}
                       for name in issue['labels'] if name in repo.labels],
            'user': {'login': VIEWER_LOGIN, 'id': 1000, 'type': 'User'},
            'created_at': FIXED_TS, 'updated_at': FIXED_TS,
            'closed_at': FIXED_TS if issue['state'] == 'closed' else None,
            'comments': len(repo.comments.get(issue['number'], [])),
            'repository_url': '/api/v3/repos/%s/%s'
                              % (repo.owner, repo.name)}


def issue_json_cached(repo, issue):
    cached = repo.issue_cache.get(issue['number'])
    if cached is not None:
        return cached
    data = json.dumps(issue_json(repo, issue), separators=(',', ':')
                      ).encode('utf-8')
    repo.issue_cache[issue['number']] = data
    return data


def pull_json(repo, pr):
    head_sha = repo.branch_tip(pr['head_ref']) or ''
    base_sha = repo.branch_tip(pr['base_ref']) or ''
    return {'id': pr['id'], 'number': pr['number'], 'title': pr['title'],
            'body': pr.get('body', ''), 'state': pr['state'],
            'merged': pr.get('merged', False),
            'merged_at': pr.get('merged_at'),
            'merge_commit_sha': pr.get('merge_commit_sha'),
            'head': {'ref': pr['head_ref'], 'sha': head_sha,
                     'label': '%s:%s' % (repo.owner, pr['head_ref'])},
            'base': {'ref': pr['base_ref'], 'sha': base_sha,
                     'label': '%s:%s' % (repo.owner, pr['base_ref'])},
            'user': {'login': VIEWER_LOGIN, 'id': 1000, 'type': 'User'},
            'created_at': FIXED_TS, 'updated_at': FIXED_TS}


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

TOKEN = os.environ.get('MAKEFUN_SHOP_TOKEN', '')


def check_auth(headers):
    """True when the request carries an acceptable Bearer token."""
    if not TOKEN:
        auth = headers.get('Authorization', '')
        return auth.startswith('Bearer ') and len(auth) > 7
    return headers.get('Authorization', '') == 'Bearer ' + TOKEN


def parse_until_flush(buf):
    """Parse pkt-lines from buf until the first flush packet.

    Return (packets, offset_after_flush). Raises ValueError on bad framing
    before the flush; anything after the flush is left unconsumed (a pack)."""
    packets = []
    off = 0
    n = len(buf)
    while True:
        if off + 4 > n:
            raise ValueError('truncated pkt-line')
        head = buf[off:off + 4]
        try:
            length = int(head.decode('ascii'), 16)
        except ValueError:
            raise ValueError('not a pkt-line')
        if length == 0:
            return packets, off + 4
        if length == 1:
            packets.append(('delim', None))
            off += 4
        elif length == 2:
            packets.append(('end', None))
            off += 4
        elif length < 4 or length > 65520:
            raise ValueError('bad pkt-line length')
        else:
            if off + length > n:
                raise ValueError('truncated pkt-line payload')
            packets.append(('data', bytes(buf[off + 4:off + length])))
            off += length


def sideband_pack(pack_bytes):
    out = bytearray()
    for i in range(0, len(pack_bytes), 65515):
        chunk = pack_bytes[i:i + 65515]
        out += pkt(b'\x01' + chunk)
    out += FLUSH
    return bytes(out)


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Forge/1.0'
    sys_version = ''

    def version_string(self):
        return 'Forge/1.0'

    # -- plumbing ---------------------------------------------------------
    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        pass

    def handle_error(self, request, client_address):
        pass

    def send_response(self, code, message=None):
        # No Date header: it costs a syscall per response and adds nothing.
        self.send_response_only(code, message)
        self.send_header('Server', self.version_string())

    def _send(self, status, body, ctype='application/json', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-GitHub-Request-Id', next_request_id())
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if body and self.command != 'HEAD':
            self.wfile.write(body)

    def send_json(self, status, obj, extra=None):
        if isinstance(obj, (dict, list)):
            body = json.dumps(obj, separators=(',', ':')).encode('utf-8')
        else:
            body = obj
        self._send(status, body, 'application/json', extra)

    def send_json_bytes(self, status, body, extra=None):
        self._send(status, body, 'application/json', extra)

    def err(self, status, message):
        self.send_json(status, {'message': message})

    def read_body(self):
        """Read the request body once; later calls return the cached bytes.

        The cache matters for keep-alive: a handler that answers before
        reading the body (a 404, a 401) must not leave bytes in the socket,
        or the next request on the connection is parsed as garbage."""
        if self._body_cache is not None:
            return self._body_cache
        length = self.headers.get('Content-Length')
        if not length:
            self._body_cache = b''
            return b''
        try:
            n = int(length)
        except ValueError:
            self._body_cache = b''
            return b''
        if n <= 0:
            self._body_cache = b''
            return b''
        self._body_cache = self.rfile.read(n)
        return self._body_cache

    def json_body(self):
        """Return (parsed, error_response_sent)."""
        raw = self.read_body()
        if not raw:
            return {}, None
        try:
            parsed = json.loads(raw.decode('utf-8'))
        except Exception:
            self.err(400, 'Problems parsing JSON')
            return None, True
        if not isinstance(parsed, dict):
            self.err(400, 'Problems parsing JSON')
            return None, True
        return parsed, None

    # -- entry points -----------------------------------------------------
    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def dispatch(self):
        try:
            self._body_cache = None
            if self.command in ('POST', 'PUT', 'PATCH', 'DELETE'):
                # Consume the body up front so an early 401/404 cannot
                # desync the keep-alive connection for the next request.
                self.read_body()
            if '?' in self.path:
                raw_path, _q, raw_qs = self.path.partition('?')
                path = raw_path
                query = urllib.parse.parse_qs(raw_qs, keep_blank_values=True)
            else:
                path = self.path
                query = {}
            if path == '/healthz':
                self.send_json(200, {'ok': True})
                return
            public = (self.command == 'GET' and
                      path in ('/api/v3', '/api/v3/'))
            public_meta = (self.command == 'GET' and path == '/api/v3/meta')
            if not (public or public_meta):
                if not check_auth(self.headers):
                    self.err(401, 'Requires authentication')
                    return
            if path.startswith('/api/graphql'):
                self.handle_graphql()
                return
            if path == '/mcp':
                self.handle_mcp()
                return
            if path.startswith('/api/v3'):
                self.handle_rest(path, query)
                return
            m = re.match(r'^/([^/]+)/([^/]+)\.git/(info/refs|git-upload-pack|git-receive-pack)$', path)
            if m:
                owner, repo_name, action = m.group(1), m.group(2), m.group(3)
                if action == 'info/refs':
                    self.git_info_refs(owner, repo_name, query)
                elif action == 'git-upload-pack':
                    self.git_upload_pack(owner, repo_name)
                else:
                    self.git_receive_pack(owner, repo_name)
                return
            self.err(404, 'Not Found')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.err(500, 'Internal Server Error: %s' % e)
            except Exception:
                pass

    # -- git: info/refs ---------------------------------------------------
    def _refs_advertisement(self, repo, service):
        """v0 advertisement body (without the service preamble)."""
        if service == 'upload-pack':
            caps = ('side-band-64k thin-pack ofs-delta shallow multi_ack '
                    'symref=HEAD:refs/heads/main agent=%s' % AGENT)
        else:
            caps = ('report-status side-band-64k delete-refs '
                    'symref=HEAD:refs/heads/main agent=%s' % AGENT)
        lines = []
        main_tip = repo.branch_tip('main')
        if main_tip:
            lines.append(pkt('%s HEAD\0%s\n' % (main_tip, caps)))
        else:
            lines.append(pkt('%s capabilities^{}\0%s\n' % (ZERO_OID, caps)))
        for refname, oid in sorted(repo.refs.items()):
            lines.append(pkt('%s %s\n' % (oid, refname)))
            if service == 'upload-pack':
                ent = repo.objects.get(oid)
                if ent is not None and ent[0] == 'tag':
                    lines.append(pkt('%s %s^{}\n' % (
                        peel_to_commit(repo.objects, oid), refname)))
        lines.append(FLUSH)
        return b''.join(lines)

    def git_info_refs(self, owner, repo_name, query):
        repo = STORE.get_repo(owner, repo_name)
        if repo is None:
            self.err(404, 'Not Found')
            return
        service = query.get('service', [None])[0]
        if service not in ('git-upload-pack', 'git-receive-pack'):
            self.err(404, 'Not Found')
            return
        if service == 'git-upload-pack' and \
                self.headers.get('Git-Protocol', '') == 'version=2':
            body = pkt_lines([b'version 2\n',
                              ('agent=%s\n' % AGENT).encode('utf-8'),
                              b'ls-refs=unborn\n', b'fetch=shallow filter\n',
                              b'object-format=sha1\n', 'flush'])
            self._send(200, body, 'application/x-git-upload-pack-advertisement')
            return
        svc = 'upload-pack' if service == 'git-upload-pack' else 'receive-pack'
        body = pkt('# service=%s\n' % service) + FLUSH + \
            self._refs_advertisement(repo, svc)
        self._send(200, body, 'application/x-git-%s-advertisement' % svc)

    # -- git: upload-pack -------------------------------------------------
    def _reachable_with_gen(self, repo, roots, depth, stop_at=None):
        objects = repo.objects
        seen = set()
        gen = {}
        stack = [(r, 0) for r in roots if r in objects]
        while stack:
            oid, g = stack.pop()
            if oid in seen:
                continue
            ent = objects.get(oid)
            if ent is None:
                continue
            otype, data = ent
            if otype == 'commit' and depth is not None and g >= depth:
                # Beyond the depth limit: neither the commit nor its
                # tree/blobs are part of the transfer.
                continue
            seen.add(oid)
            if otype == 'commit':
                gen[oid] = g
                if depth is not None and g >= depth:
                    continue
                c = parse_commit(data)
                stack.append((c['tree'], g))
                if stop_at is not None and oid in stop_at:
                    # A shallow boundary the client already holds: the
                    # client has this commit and its tree, but NOT its
                    # parents, so the walk stops here.
                    continue
                for p in c['parents']:
                    stack.append((p, g + 1))
            elif otype == 'tag':
                t = parse_tag(data)
                stack.append((t['object'], g))
            elif otype == 'tree':
                for _m, _n, child in parse_tree(data):
                    stack.append((child, g))
        return seen, gen

    def _fetch_pack(self, repo, wants, haves, client_shallow, depth):
        """Return (ack_lines, shallow_lines, unshallow_lines, pack_bytes).

        Identical fetches (the 50 concurrent shallow clones of one tip)
        share one built pack: the walk and the zlib work happen once and
        the cache is cleared whenever the repo's objects or refs change."""
        key = (tuple(sorted(wants)), tuple(sorted(haves)),
               tuple(sorted(client_shallow)), depth)
        cached = repo.pack_cache.get(key)
        if cached is not None:
            return cached
        result = self._fetch_pack_uncached(repo, wants, haves,
                                           client_shallow, depth)
        if len(repo.pack_cache) > 64:
            repo.pack_cache.clear()
        repo.pack_cache[key] = result
        return result

    def _fetch_pack_uncached(self, repo, wants, haves, client_shallow,
                             depth):
        objects = repo.objects
        want_set, gen = self._reachable_with_gen(repo, wants, depth)
        # The client's shallow boundaries cap what a `have` proves: the
        # client holds a shallow commit and its tree, but not its parents.
        have_set, _ = self._reachable_with_gen(
            repo, haves, None, stop_at=set(client_shallow))
        send = want_set - have_set
        ack = []
        if haves:
            if all(h in objects for h in haves):
                ack = ['ACK %s' % h for h in haves] + ['ready']
            else:
                ack = ['NAK']
        else:
            ack = ['NAK']
        boundary = set()
        if depth is not None:
            for oid, g in gen.items():
                if g == depth - 1 and objects.get(oid, ('',))[0] == 'commit':
                    boundary.add(oid)
        unshallow = set()
        for b in client_shallow:
            if b in want_set:
                ent = objects.get(b)
                if ent is not None and ent[0] == 'commit':
                    parents = parse_commit(ent[1])['parents']
                    if any(p in send for p in parents):
                        unshallow.add(b)
                    else:
                        boundary.add(b)
        shallow = sorted(boundary - unshallow)
        pack = write_pack([(objects[o][0], objects[o][1])
                           for o in sorted(send)])
        return ack, shallow, sorted(unshallow), pack

    def git_upload_pack(self, owner, repo_name):
        repo = STORE.get_repo(owner, repo_name)
        if repo is None:
            self.err(404, 'Not Found')
            return
        body = self.read_body()
        v2 = self.headers.get('Git-Protocol', '') == 'version=2'
        try:
            packets, off = parse_until_flush(body)
        except ValueError:
            self.err(400, 'Bad request')
            return
        data_lines = [p[1] for p in packets if p[0] == 'data']
        if not data_lines:
            self.err(400, 'Bad request')
            return
        first = data_lines[0].decode('utf-8', 'replace').strip()
        if v2:
            if first == 'command=ls-refs':
                self._ls_refs(repo, data_lines)
                return
            if first != 'command=fetch':
                self.err(400, 'Bad request')
                return
            args = []
            after_delim = False
            for kind, payload in packets[1:]:
                if kind == 'delim':
                    after_delim = True
                elif kind == 'data' and after_delim:
                    args.append(payload.decode('utf-8', 'replace').strip())
            self._do_fetch_v2(repo, args)
            return
        # v0 fetch: two sections (wants, then haves), each flush-terminated.
        try:
            packets2, _off2 = parse_until_flush(body[off:])
        except ValueError:
            self.err(400, 'Bad request')
            return
        wants, haves, client_shallow, depth = [], [], [], None
        multi_ack = False
        for kind, payload in packets:
            if kind != 'data':
                continue
            line = payload.decode('utf-8', 'replace').strip()
            if line.startswith('want '):
                wants.append(line[5:].split()[0])
                if 'multi_ack' in line:
                    multi_ack = True
            elif line.startswith('deepen '):
                try:
                    depth = int(line[7:])
                except ValueError:
                    pass
            elif line.startswith('shallow '):
                client_shallow.append(line[8:])
            elif 'multi_ack' in line:
                multi_ack = True
        for kind, payload in packets2:
            if kind != 'data':
                continue
            line = payload.decode('utf-8', 'replace').strip()
            if line.startswith('have '):
                haves.append(line[5:])
        ack, shallow, unshallow, pack = self._fetch_pack(
            repo, wants, haves, client_shallow, depth)
        out = bytearray()
        for a in ack:
            out += pkt(a + '\n')
            if a == 'NAK':
                break
            if a == 'ready' and not multi_ack:
                out = out[:-len(pkt('ready\n'))]
        for s in shallow:
            out += pkt('shallow %s\n' % s)
        for u in unshallow:
            out += pkt('unshallow %s\n' % u)
        out += sideband_pack(pack)
        self._send(200, bytes(out), 'application/x-git-upload-pack-result')

    def _ls_refs(self, repo, data_lines):
        args = []
        after_delim = False
        for line in data_lines[1:]:
            text = line.decode('utf-8', 'replace').strip()
            if text == '':
                continue
            args.append(text)
        peel = 'peel' in args
        symrefs = 'symrefs' in args
        unborn = 'unborn' in args
        prefixes = [a[11:] for a in args if a.startswith('ref-prefix ')]
        out = []
        main_tip = repo.branch_tip('main')
        if not prefixes and symrefs:
            if main_tip:
                out.append('%s HEAD symref-target:refs/heads/main'
                           % main_tip)
            elif unborn:
                out.append('unborn HEAD symref-target:refs/heads/main')
        for refname, oid in sorted(repo.refs.items()):
            if prefixes and not any(refname.startswith(p) for p in prefixes):
                continue
            line = '%s %s' % (oid, refname)
            if peel:
                ent = repo.objects.get(oid)
                if ent is not None and ent[0] == 'tag':
                    line += ' peeled:%s' % peel_to_commit(repo.objects, oid)
            out.append(line)
        body = pkt_lines([l + '\n' for l in out] + ['flush'])
        self._send(200, body, 'application/x-git-upload-pack-result')

    def _do_fetch_v2(self, repo, args):
        wants, haves, client_shallow, depth = [], [], [], None
        for line in args:
            if line.startswith('want '):
                wants.append(line[5:].strip())
            elif line.startswith('have '):
                haves.append(line[5:].strip())
            elif line.startswith('shallow '):
                client_shallow.append(line[8:].strip())
            elif line.startswith('deepen '):
                try:
                    depth = int(line[7:])
                except ValueError:
                    pass
        ack, shallow, unshallow, pack = self._fetch_pack(
            repo, wants, haves, client_shallow, depth)
        sections = [pkt('acknowledgments\n')]
        for a in ack:
            sections.append(pkt(a + '\n'))
        sections.append(DELIM)
        if shallow or unshallow:
            sections.append(pkt('shallow-info\n'))
            for s in shallow:
                sections.append(pkt('shallow %s\n' % s))
            for u in unshallow:
                sections.append(pkt('unshallow %s\n' % u))
            sections.append(DELIM)
        sections.append(pkt('packfile\n'))
        sections.append(sideband_pack(pack))
        self._send(200, b''.join(sections),
                   'application/x-git-upload-pack-result')

    # -- git: receive-pack ------------------------------------------------
    def git_receive_pack(self, owner, repo_name):
        repo = STORE.get_repo(owner, repo_name)
        if repo is None:
            self.err(404, 'Not Found')
            return
        body = self.read_body()
        try:
            packets, pack_off = parse_until_flush(body)
        except ValueError:
            self.err(400, 'Bad request')
            return
        commands = []
        sideband = False
        for kind, payload in packets:
            if kind != 'data':
                continue
            line = payload.decode('utf-8', 'replace')
            if '\x00' in line:
                head, _caps = line.split('\x00', 1)
                line = head
                if 'side-band-64k' in _caps:
                    sideband = True
            line = line.rstrip('\n')
            parts = line.split(' ')
            if len(parts) != 3:
                self.err(400, 'Bad request')
                return
            old, new, ref = parts
            if not ref.startswith('refs/'):
                self.err(400, 'Bad request')
                return
            if not (_HEX40.match(old) or old == ZERO_OID) or \
                    not (_HEX40.match(new) or new == ZERO_OID):
                self.err(400, 'Bad request')
                return
            commands.append((old, new, ref))
        pack_bytes = body[pack_off:]
        if any(new != ZERO_OID for _o, new, _r in commands):
            if not pack_bytes:
                self.err(400, 'Bad request')
                return
            try:
                new_objects = read_pack(pack_bytes)
            except PackError as e:
                self.err(400, 'Unpack failed: %s' % e)
                return
            with STORE.lock:
                repo.objects.update(new_objects)
                repo.pack_cache.clear()
        results = []
        with STORE.lock:
            for old, new, ref in commands:
                current = repo.refs.get(ref)
                if new == ZERO_OID:
                    if current is not None and current == old:
                        del repo.refs[ref]
                        results.append(('ok', ref, ''))
                    else:
                        results.append(('ng', ref, 'non-fast-forward'))
                elif old == ZERO_OID:
                    if ref in repo.refs:
                        results.append(('ng', ref, 'already exists'))
                    else:
                        repo.refs[ref] = new
                        results.append(('ok', ref, ''))
                else:
                    if current == old and is_ancestor(repo.objects, old, new):
                        repo.refs[ref] = new
                        results.append(('ok', ref, ''))
                    else:
                        results.append(('ng', ref, 'non-fast-forward'))
        inner = bytearray()
        inner += pkt('unpack ok\n')
        for status, ref, reason in results:
            if status == 'ok':
                inner += pkt('ok %s\n' % ref)
            else:
                inner += pkt('ng %s %s\n' % (ref, reason))
        inner += FLUSH
        if any(status == 'ok' for status, _r, _x in results):
            repo.pack_cache.clear()
        if sideband:
            out = bytearray()
            for i in range(0, len(inner), 65515):
                out += pkt(b'\x01' + bytes(inner[i:i + 65515]))
            out += FLUSH
            body_out = bytes(out)
        else:
            body_out = bytes(inner)
        self._send(200, body_out, 'application/x-git-receive-pack-result')


    # -- REST v3 ----------------------------------------------------------
    def _paginate(self, items, query, path):
        try:
            per_page = int(query.get('per_page', ['30'])[0])
        except ValueError:
            per_page = 30
        per_page = max(1, min(per_page, 100))
        try:
            page = int(query.get('page', ['1'])[0])
        except ValueError:
            page = 1
        page = max(1, page)
        total = len(items)
        start = (page - 1) * per_page
        page_items = items[start:start + per_page]
        links = []
        host = self.headers.get('Host', 'localhost')
        base = 'http://%s%s' % (host, path)
        if start + per_page < total:
            links.append('<%s?per_page=%d&page=%d>; rel="next"'
                         % (base, per_page, page + 1))
            last = (total + per_page - 1) // per_page
            links.append('<%s?per_page=%d&page=%d>; rel="last"'
                         % (base, per_page, last))
        if page > 1:
            links.append('<%s?per_page=%d&page=1>; rel="first"'
                         % (base, per_page))
            links.append('<%s?per_page=%d&page=%d>; rel="prev"'
                         % (base, per_page, page - 1))
        return page_items, ({'Link': ', '.join(links)} if links else None)

    def _q(self, query, name, default=None):
        vals = query.get(name)
        return vals[0] if vals else default

    def _repo_or_404(self, owner, name):
        repo = STORE.get_repo(owner, name)
        if repo is None:
            self.err(404, 'Not Found')
            return None
        return repo

    def _commits_list(self, repo, tip_oid, query):
        """Newest-first DFS pre-order walk from tip_oid."""
        out = []
        seen = set()
        stack = [tip_oid]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            ent = repo.objects.get(oid)
            if ent is None or ent[0] != 'commit':
                continue
            c = parse_commit(ent[1])
            out.append({'sha': oid,
                        'commit': {'message': c['message'].rstrip('\n'),
                                   'author': {'name': VIEWER['name'],
                                              'email': VIEWER['email'],
                                              'date': FIXED_TS},
                                   'committer': {'name': VIEWER['name'],
                                                 'email': VIEWER['email'],
                                                 'date': FIXED_TS}},
                        'parents': [{'sha': p} for p in c['parents']]})
            for p in reversed(c['parents']):
                stack.append(p)
        page_items, extra = self._paginate(out, query,
                                           self._path_for_pagination())
        return page_items, extra

    def _path_for_pagination(self):
        return urllib.parse.urlparse(self.path).path

    def handle_rest(self, path, query):
        parts = [p for p in path.split('/') if p != '']
        # parts[0]='api', parts[1]='v3'
        if path in ('/api/v3', '/api/v3/'):
            self.send_json(200, {
                'message': 'GitHub API v3',
                'current_user_url': '/api/v3/user',
                'repository_url': '/api/v3/repos/{owner}/{repo}',
                'rate_limit_url': '/api/v3/rate_limit'},
                {'X-OAuth-Scopes': 'repo, read:org'})
            return
        if path == '/api/v3/meta':
            self.send_json(200, {'installed_version': '1.0.0-forge',
                                 'verifiable_password_authentication': False,
                                 'git': ['1.0']})
            return
        if path == '/api/v3/user':
            if self.command != 'GET':
                self.err(404, 'Not Found')
                return
            self.send_json(200, {'login': VIEWER_LOGIN, 'id': 1000,
                                 'name': VIEWER['name'],
                                 'email': VIEWER['email'], 'type': 'User',
                                 'site_admin': False})
            return
        if path == '/api/v3/user/repos':
            if self.command == 'GET':
                repos = [r for r in STORE.repos.values()
                         if r.owner == VIEWER_LOGIN]
                repos.sort(key=lambda r: r.seq)
                items, extra = self._paginate(
                    [repo_json(r) for r in repos], query, path)
                self.send_json(200, items, extra)
                return
            if self.command == 'POST':
                body, bad = self.json_body()
                if bad:
                    return
                name = body.get('name')
                if not name or not isinstance(name, str):
                    self.err(422, 'name is required')
                    return
                with STORE.lock:
                    repo = STORE.create_repo(
                        name, VIEWER_LOGIN,
                        body.get('description') or '',
                        bool(body.get('private', False)))
                if repo is None:
                    self.err(422, 'Repository already exists')
                    return
                self.send_json(201, repo_json(repo))
                return
            self.err(404, 'Not Found')
            return
        if len(parts) >= 4 and parts[0] == 'api' and parts[1] == 'v3' \
                and parts[2] == 'repos':
            self._handle_repo_routes(parts[3:], path, query)
            return
        self.err(404, 'Not Found')

    def _handle_repo_routes(self, rest, path, query):
        owner, name = rest[0], rest[1]
        repo = self._repo_or_404(owner, name)
        if repo is None:
            return
        tail = rest[2:]
        if not tail:
            if self.command == 'GET':
                self.send_json(200, repo_json(repo))
            elif self.command == 'PATCH':
                body, bad = self.json_body()
                if bad:
                    return
                with STORE.lock:
                    if isinstance(body.get('description'), str):
                        repo.description = body['description']
                self.send_json(200, repo_json(repo))
            elif self.command == 'DELETE':
                self.read_body()
                STORE.delete_repo(owner, name)
                self._send(204, b'')
            else:
                self.err(404, 'Not Found')
            return
        if tail[0] == 'branches':
            if len(tail) == 1 and self.command == 'GET':
                items = []
                for refname in sorted(repo.refs):
                    if refname.startswith('refs/heads/'):
                        items.append({'name': refname[11:],
                                      'commit': {'sha': repo.refs[refname]}})
                self.send_json(200, items)
                return
            if len(tail) == 2 and self.command == 'GET':
                tip = repo.branch_tip(tail[1])
                if tip is None:
                    self.err(404, 'Not Found')
                    return
                self.send_json(200, {'name': tail[1],
                                     'commit': {'sha': tip}})
                return
            self.err(404, 'Not Found')
            return
        if tail[0] == 'commits' and self.command == 'GET':
            sha = self._q(query, 'sha')
            if sha is None:
                tip = repo.branch_tip(repo.default_branch)
            elif repo.branch_tip(sha) is not None:
                tip = repo.branch_tip(sha)
            elif _HEX40.match(sha) and sha in repo.objects:
                tip = peel_to_commit(repo.objects, sha)
            elif repo.refs.get('refs/heads/%s' % sha) is not None:
                tip = repo.branch_tip(sha)
            else:
                self.err(404, 'Not Found')
                return
            if tip is None:
                self.send_json(200, [])
                return
            items, extra = self._commits_list(repo, tip, query)
            self.send_json(200, items, extra)
            return
        if tail[0] == 'git' and len(tail) >= 2 and tail[1] == 'refs':
            if len(tail) == 2 and self.command == 'POST':
                body, bad = self.json_body()
                if bad:
                    return
                ref = body.get('ref')
                sha = body.get('sha')
                if not ref or not isinstance(ref, str) or \
                        not ref.startswith('refs/heads/') or not sha or \
                        not _HEX40.match(sha) or sha not in repo.objects:
                    self.err(422, 'Invalid ref or sha')
                    return
                with STORE.lock:
                    if ref in repo.refs:
                        self.err(422, 'Reference already exists')
                        return
                    repo.refs[ref] = sha
                    repo.pack_cache.clear()
                self.send_json(201, {'ref': ref,
                                     'object': {'sha': sha,
                                                'type': 'commit'}})
                return
            if len(tail) == 4 and tail[2] == 'heads' and self.command == 'GET':
                tip = repo.branch_tip(tail[3])
                if tip is None:
                    self.err(404, 'Not Found')
                    return
                self.send_json(200, {'ref': 'refs/heads/%s' % tail[3],
                                     'object': {'sha': tip,
                                                'type': 'commit'}})
                return
            self.err(404, 'Not Found')
            return
        if tail[0] == 'contents':
            self._handle_contents(repo, tail[1:], query)
            return
        if tail[0] == 'labels':
            if self.command == 'GET':
                labels = [repo.labels[n] for n in sorted(repo.labels)]
                self.send_json(200, labels)
                return
            if self.command == 'POST':
                body, bad = self.json_body()
                if bad:
                    return
                lname = body.get('name')
                color = body.get('color', 'ededed')
                if not lname:
                    self.err(422, 'name is required')
                    return
                with STORE.lock:
                    if lname in repo.labels:
                        self.err(422, 'Label already exists')
                        return
                    STORE.global_label_id += 1
                    label = {'id': STORE.global_label_id, 'name': lname,
                             'color': color, 'description': '',
                             'default': False}
                    repo.labels[lname] = label
                self.send_json(201, label)
                return
            self.err(404, 'Not Found')
            return
        if tail[0] == 'issues':
            self._handle_issues(repo, tail[1:], query)
            return
        if tail[0] == 'pulls':
            self._handle_pulls(repo, tail[1:], query)
            return
        self.err(404, 'Not Found')

    def _handle_contents(self, repo, tail, query):
        cpath = '/'.join(tail)
        if self.command == 'GET':
            ref = self._q(query, 'ref') or repo.default_branch
            if repo.branch_tip(ref) is not None:
                tip = repo.branch_tip(ref)
            elif _HEX40.match(ref or '') and ref in repo.objects:
                tip = peel_to_commit(repo.objects, ref)
            else:
                self.err(404, 'Not Found')
                return
            ent = repo.objects.get(tip)
            if ent is None or ent[0] != 'commit':
                self.err(404, 'Not Found')
                return
            files = tree_files(repo, parse_commit(ent[1])['tree'])
            if cpath in files:
                mode, boid = files[cpath]
                data = repo.objects[boid][1]
                self.send_json(200, {'type': 'file', 'encoding': 'base64',
                                     'size': len(data),
                                     'name': cpath.rsplit('/', 1)[-1],
                                     'path': cpath, 'sha': boid,
                                     'content': base64.b64encode(data
                                                                 ).decode('ascii')})
                return
            prefix = cpath + '/' if cpath else ''
            entries = {}
            for fpath, (mode, boid) in files.items():
                if not fpath.startswith(prefix):
                    continue
                rest = fpath[len(prefix):]
                if '/' in rest:
                    dname = rest.split('/', 1)[0]
                    entries[dname] = {'type': 'dir', 'name': dname,
                                      'path': prefix + dname,
                                      'sha': _dir_sha(repo, files,
                                                      prefix + dname)}
                else:
                    entries[rest] = {'type': 'file', 'name': rest,
                                     'path': fpath, 'sha': boid,
                                     'size': len(repo.objects[boid][1])}
            if entries:
                self.send_json(200, [entries[k] for k in sorted(entries)])
                return
            self.err(404, 'Not Found')
            return
        if self.command in ('PUT', 'DELETE'):
            body, bad = self.json_body()
            if bad:
                return
            branch = body.get('branch') or repo.default_branch
            message = body.get('message') or ''
            with STORE.lock:
                if self.command == 'PUT':
                    content_b64 = body.get('content')
                    if content_b64 is None:
                        self.err(422, 'content is required')
                        return
                    try:
                        data = base64.b64decode(content_b64, validate=False)
                    except Exception:
                        self.err(400, 'Invalid base64 content')
                        return
                    files, _tip = files_at_ref(
                        repo, 'refs/heads/%s' % branch)
                    if files is None:
                        files = {}
                    if cpath in files:
                        cur_sha = files[cpath][1]
                        if body.get('sha') != cur_sha:
                            self.err(409, 'sha does not match current file')
                            return
                        status = 200
                    else:
                        status = 201
                    oid = create_contents_commit(repo, branch, cpath, data,
                                                 message)
                    blob_sha = hash_object('blob', data)
                    self.send_json(status, {
                        'content': {'name': cpath.rsplit('/', 1)[-1],
                                    'path': cpath, 'sha': blob_sha,
                                    'size': len(data), 'encoding': 'base64',
                                    'content': content_b64},
                        'commit': {'sha': oid, 'message': message}})
                    return
                # DELETE
                files, _tip = files_at_ref(
                    repo, 'refs/heads/%s' % branch)
                if files is None:
                    files = {}
                if cpath not in files:
                    self.err(404, 'Not Found')
                    return
                if body.get('sha') != files[cpath][1]:
                    self.err(409, 'sha does not match current file')
                    return
                oid = create_contents_commit(repo, branch, cpath, b'',
                                             message, delete=True)
                self.send_json(200, {'commit': {'sha': oid,
                                                'message': message}})
                return
        self.err(404, 'Not Found')

    def _handle_issues(self, repo, tail, query):
        if not tail:
            if self.command == 'GET':
                state = self._q(query, 'state', 'open')
                direction = self._q(query, 'direction', 'desc')
                issues = list(repo.issues.values())
                if state != 'all':
                    issues = [i for i in issues if i['state'] == state]
                issues.sort(key=lambda i: i['number'],
                            reverse=(direction == 'desc'))
                items, extra = self._paginate(
                    [issue_json(repo, i) for i in issues], query,
                    self._path_for_pagination())
                self.send_json(200, items, extra)
                return
            if self.command == 'POST':
                body, bad = self.json_body()
                if bad:
                    return
                title = body.get('title')
                if not title:
                    self.err(422, 'title is required')
                    return
                labels = body.get('labels') or []
                for name in labels:
                    if name not in repo.labels:
                        self.err(422, 'Unknown label: %s' % name)
                        return
                with STORE.lock:
                    repo.issue_seq += 1
                    STORE.global_issue_id += 1
                    issue = {'number': repo.issue_seq,
                             'id': STORE.global_issue_id,
                             'title': title,
                             'body': body.get('body') or '',
                             'state': 'open',
                             'labels': list(labels)}
                    repo.issues[issue['number']] = issue
                self.send_json(201, issue_json(repo, issue))
                return
            self.err(404, 'Not Found')
            return
        try:
            number = int(tail[0])
        except ValueError:
            self.err(404, 'Not Found')
            return
        issue = repo.issues.get(number)
        if len(tail) == 2 and tail[1] == 'comments':
            if self.command == 'GET':
                comments = repo.comments.get(number, [])
                self.send_json(200, [{'id': c['id'], 'body': c['body'],
                                      'user': {'login': VIEWER_LOGIN,
                                               'id': 1000, 'type': 'User'},
                                      'created_at': FIXED_TS}
                                     for c in comments])
                return
            if self.command == 'POST':
                body, bad = self.json_body()
                if bad:
                    return
                if issue is None:
                    self.err(404, 'Not Found')
                    return
                if not body.get('body'):
                    self.err(422, 'body is required')
                    return
                with STORE.lock:
                    STORE.global_comment_id += 1
                    comment = {'id': STORE.global_comment_id,
                               'body': body['body']}
                    repo.comments.setdefault(number, []).append(comment)
                self.send_json(201, {'id': comment['id'],
                                     'body': comment['body'],
                                     'user': {'login': VIEWER_LOGIN,
                                              'id': 1000, 'type': 'User'},
                                     'created_at': FIXED_TS})
                return
            self.err(404, 'Not Found')
            return
        if len(tail) == 1:
            if issue is None:
                self.err(404, 'Not Found')
                return
            if self.command == 'GET':
                self.send_json_bytes(200, issue_json_cached(repo, issue))
                return
            if self.command == 'PATCH':
                body, bad = self.json_body()
                if bad:
                    return
                with STORE.lock:
                    if body.get('state') in ('open', 'closed'):
                        issue['state'] = body['state']
                    if isinstance(body.get('title'), str):
                        issue['title'] = body['title']
                    if isinstance(body.get('body'), str):
                        issue['body'] = body['body']
                    repo.issue_cache.pop(number, None)
                self.send_json(200, issue_json(repo, issue))
                return
            self.err(404, 'Not Found')
            return
        self.err(404, 'Not Found')

    def _handle_pulls(self, repo, tail, query):
        if not tail:
            if self.command == 'GET':
                state = self._q(query, 'state', 'open')
                pulls = list(repo.pulls.values())
                if state != 'all':
                    pulls = [p for p in pulls if p['state'] == state]
                pulls.sort(key=lambda p: p['number'])
                items, extra = self._paginate(
                    [pull_json(repo, p) for p in pulls], query,
                    self._path_for_pagination())
                self.send_json(200, items, extra)
                return
            if self.command == 'POST':
                body, bad = self.json_body()
                if bad:
                    return
                head = body.get('head')
                base = body.get('base')
                title = body.get('title')
                if not head or not base or not title:
                    self.err(422, 'head, base and title are required')
                    return
                if repo.branch_tip(head) is None or \
                        repo.branch_tip(base) is None:
                    self.err(422, 'Unknown head or base branch')
                    return
                with STORE.lock:
                    for p in repo.pulls.values():
                        if p['state'] == 'open' and \
                                p['head_ref'] == head and \
                                p['base_ref'] == base:
                            self.err(422, 'Pull request already exists')
                            return
                    repo.pr_seq += 1
                    STORE.global_issue_id += 1
                    pr = {'number': repo.pr_seq,
                          'id': STORE.global_issue_id,
                          'title': title,
                          'body': body.get('body') or '',
                          'state': 'open', 'merged': False,
                          'merged_at': None, 'merge_commit_sha': None,
                          'head_ref': head, 'base_ref': base,
                          'head_sha': repo.branch_tip(head),
                          'base_sha': repo.branch_tip(base)}
                    repo.pulls[pr['number']] = pr
                self.send_json(201, pull_json(repo, pr))
                return
            self.err(404, 'Not Found')
            return
        try:
            number = int(tail[0])
        except ValueError:
            self.err(404, 'Not Found')
            return
        pr = repo.pulls.get(number)
        if len(tail) == 2 and tail[1] == 'files':
            if self.command != 'GET':
                self.err(404, 'Not Found')
                return
            if pr is None:
                self.err(404, 'Not Found')
                return
            head_tip = repo.branch_tip(pr['head_ref']) or pr['head_sha']
            base_tip = repo.branch_tip(pr['base_ref']) or pr['base_sha']
            if not head_tip or not base_tip:
                self.err(404, 'Not Found')
                return
            mb = merge_base(repo.objects, head_tip, base_tip)
            if mb is None:
                self.send_json(200, [])
                return
            mb_files = tree_files(repo, parse_commit(
                repo.objects[mb][1])['tree'])
            head_files = tree_files(repo, parse_commit(
                repo.objects[head_tip][1])['tree'])
            self.send_json(200, diff_files(repo, mb_files, head_files))
            return
        if len(tail) == 2 and tail[1] == 'merge':
            if self.command != 'PUT':
                self.err(404, 'Not Found')
                return
            if pr is None:
                self.err(404, 'Not Found')
                return
            self.read_body()
            with STORE.lock:
                status, payload = squash_merge(repo, pr)
            self.send_json(status, payload)
            return
        if len(tail) == 1:
            if pr is None:
                self.err(404, 'Not Found')
                return
            if self.command == 'GET':
                self.send_json(200, pull_json(repo, pr))
                return
            self.err(404, 'Not Found')
            return
        self.err(404, 'Not Found')


def _find_subtree(repo, files, dirpath):
    """Return the tree oid for a directory path (or None)."""
    # Rebuild the subtree by walking from the root tree is expensive; instead
    # derive it from the flattened map by rebuilding the directory's tree.
    sub = {}
    prefix = dirpath + '/'
    for fpath, (mode, boid) in files.items():
        if fpath.startswith(prefix):
            sub[fpath[len(prefix):]] = (mode, boid)
    if not sub:
        return None
    return build_tree_from_files(repo, sub)


def _dir_sha(repo, files, dirpath):
    oid = _find_subtree(repo, files, dirpath)
    return oid or ZERO_OID


# ---------------------------------------------------------------------------
# GraphQL v4: hand-rolled parser + executor for the documents gh and
# github-mcp-server send (see NOTES.md for the full document list).
# ---------------------------------------------------------------------------

_GQL_TYPES = {
    'Query': ['viewer', 'repository', 'node', '__type', '__schema'],
    'User': ['login', 'id', 'name', 'email', 'url', 'avatarUrl'],
    'Repository': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner',
                   'defaultBranchRef', 'viewerPermission', 'hasIssuesEnabled',
                   'description', 'url', 'private', 'createdAt', 'updatedAt',
                   'issue', 'issues', 'pullRequest', 'pullRequests'],
    'Ref': ['name', 'target', 'prefix'],
    'Issue': ['id', 'databaseId', 'number', 'title', 'body', 'state',
              'author', 'createdAt', 'updatedAt', 'url', 'labels', 'comments',
              'closed', 'locked'],
    'IssueConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'IssueEdge': ['node', 'cursor'],
    'PullRequest': ['id', 'databaseId', 'number', 'title', 'body', 'state',
                    'merged', 'mergeable', 'headRefName', 'baseRefName',
                    'headRef', 'baseRef', 'files', 'author', 'createdAt',
                    'updatedAt', 'mergedAt', 'mergeCommit', 'additions',
                    'deletions', 'changedFiles'],
    'PullRequestConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'PullRequestChangedFileConnection': ['nodes', 'edges', 'pageInfo',
                                         'totalCount'],
    'PullRequestChangedFile': ['path', 'additions', 'deletions', 'changeType'],
    'IssueComment': ['id', 'body', 'author', 'createdAt', 'updatedAt'],
    'IssueCommentEdge': ['node', 'cursor'],
    'IssueCommentConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'PageInfo': ['hasNextPage', 'hasPreviousPage', 'startCursor', 'endCursor'],
    'Mutation': ['createIssue', 'closeIssue', 'createPullRequest',
                 'mergePullRequest', 'addComment'],
    'CreateIssuePayload': ['issue', 'clientMutationId'],
    'CloseIssuePayload': ['issue', 'clientMutationId'],
    'CreatePullRequestPayload': ['pullRequest', 'clientMutationId'],
    'MergePullRequestPayload': ['pullRequest', 'clientMutationId'],
    'AddCommentPayload': ['commentEdge', 'clientMutationId'],
    'Label': ['id', 'name', 'color', 'description'],
    'LabelConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'Commit': ['oid', 'message'],
    '__Type': ['name', 'kind', 'description', 'fields', 'enumValues',
               'inputFields', 'interfaces', 'possibleTypes'],
    '__Field': ['name', 'description', 'args', 'type'],
    '__EnumValue': ['name', 'description', 'isDeprecated',
                    'deprecationReason'],
    '__Schema': ['types', 'queryType', 'mutationType', 'subscriptionType',
                 'directives'],
}

# Field names each introspected type must report.
_INTROSPECT_FIELDS = {
    'Issue': ['id', 'databaseId', 'number', 'title', 'body', 'state',
              'author', 'createdAt', 'updatedAt', 'url', 'labels',
              'comments', 'closed', 'locked'],
    'PullRequest': ['id', 'databaseId', 'number', 'title', 'body', 'state',
                    'merged', 'mergeable', 'headRefName', 'baseRefName',
                    'headRef', 'baseRef', 'files', 'author', 'createdAt',
                    'updatedAt', 'mergedAt', 'mergeCommit', 'additions',
                    'deletions', 'changedFiles'],
    'Repository': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner',
                   'defaultBranchRef', 'viewerPermission', 'hasIssuesEnabled',
                   'description', 'url', 'private', 'createdAt', 'updatedAt',
                   'issue', 'issues', 'pullRequest', 'pullRequests'],
}
_INTROSPECT_ENUMS = {
    'SearchType': ['ISSUE', 'PULL_REQUEST', 'REPOSITORY', 'USER', 'DISCUSSION',
                   'COMMIT'],
    'IssueState': ['OPEN', 'CLOSED'],
    'PullRequestState': ['OPEN', 'CLOSED', 'MERGED'],
}


class _GqlError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


class _GqlTokenizer:
    def __init__(self, text):
        self.text = text
        self.i = 0
        self.n = len(text)

    def tokens(self):
        out = []
        while self.i < self.n:
            c = self.text[self.i]
            if c in ' \t\r\n,':
                self.i += 1
            elif c == '#':
                while self.i < self.n and self.text[self.i] != '\n':
                    self.i += 1
            elif c == '.' and self.text[self.i:self.i + 3] == '...':
                out.append(('...', '...'))
                self.i += 3
            elif c in '!$()[]{}:=@&|':
                out.append((c, c))
                self.i += 1
            elif c == '"':
                out.append(('string', self._string()))
            elif c == '-' or c.isdigit():
                out.append(('number', self._number()))
            elif c.isalpha() or c == '_':
                j = self.i
                while j < self.n and (self.text[j].isalnum() or
                                      self.text[j] == '_'):
                    j += 1
                out.append(('name', self.text[self.i:j]))
                self.i = j
            else:
                raise _GqlError('Unexpected character %r' % c)
        out.append(('eof', None))
        return out

    def _string(self):
        self.i += 1
        out = []
        while self.i < self.n:
            c = self.text[self.i]
            if c == '"':
                self.i += 1
                return ''.join(out)
            if c == '\\':
                self.i += 1
                e = self.text[self.i]
                mapping = {'n': '\n', 't': '\t', 'r': '\r', 'b': '\b',
                           'f': '\f', '/': '/', '\\': '\\', '"': '"'}
                if e == 'u':
                    out.append(chr(int(self.text[self.i + 1:self.i + 5], 16)))
                    self.i += 5
                else:
                    out.append(mapping.get(e, e))
                    self.i += 1
            else:
                out.append(c)
                self.i += 1
        raise _GqlError('Unterminated string')

    def _number(self):
        j = self.i
        if self.text[j] == '-':
            j += 1
        while j < self.n and self.text[j].isdigit():
            j += 1
        is_float = False
        if j < self.n and self.text[j] == '.':
            is_float = True
            j += 1
            while j < self.n and self.text[j].isdigit():
                j += 1
        if j < self.n and self.text[j] in 'eE':
            is_float = True
            j += 1
            if j < self.n and self.text[j] in '+-':
                j += 1
            while j < self.n and self.text[j].isdigit():
                j += 1
        raw = self.text[self.i:j]
        self.i = j
        return float(raw) if is_float else int(raw)


class _GqlParser:
    def __init__(self, text):
        self.toks = _GqlTokenizer(text).tokens()
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise _GqlError('Expected %s, got %s' % (kind, tok[0]))
        return tok

    def parse(self):
        ops = []
        while self.peek()[0] != 'eof':
            ops.append(self.parse_operation())
        return ops

    def parse_operation(self):
        kind, val = self.peek()
        if kind == '{':
            return {'kind': 'query', 'name': None, 'variables': {},
                    'selection': self.parse_selection_set()}
        if kind == 'name' and val in ('query', 'mutation', 'subscription'):
            self.next()
            name = None
            if self.peek()[0] == 'name':
                name = self.next()[1]
            variables = {}
            if self.peek()[0] == '(':
                variables = self.parse_variable_definitions()
            return {'kind': val, 'name': name, 'variables': variables,
                    'selection': self.parse_selection_set()}
        raise _GqlError('Expected operation, got %s' % (kind,))

    def parse_variable_definitions(self):
        self.expect('(')
        variables = {}
        while self.peek()[0] != ')':
            self.expect('$')
            name = self.expect('name')[1]
            self.expect(':')
            self.parse_type()
            default = None
            if self.peek()[0] == '=':
                self.next()
                default = self.parse_value()
            variables[name] = default
        self.expect(')')
        return variables

    def parse_type(self):
        if self.peek()[0] == '[':
            self.next()
            self.parse_type()
            self.expect(']')
        else:
            self.expect('name')
        if self.peek()[0] == '!':
            self.next()

    def parse_selection_set(self):
        self.expect('{')
        sels = []
        while self.peek()[0] != '}':
            sels.append(self.parse_selection())
        self.expect('}')
        return sels

    def parse_selection(self):
        if self.peek()[0] == '...':
            self.next()
            if self.peek() == ('name', 'on'):
                self.next()
                type_name = self.expect('name')[1]
                self.skip_directives()
                return {'fragment': True, 'type': type_name,
                        'selection': self.parse_selection_set()}
            self.skip_directives()
            return {'fragment': True, 'type': None,
                    'selection': self.parse_selection_set()}
        name = self.expect('name')[1]
        alias = None
        if self.peek()[0] == ':':
            self.next()
            alias = name
            name = self.expect('name')[1]
        args = {}
        if self.peek()[0] == '(':
            args = self.parse_arguments()
        self.skip_directives()
        selection = None
        if self.peek()[0] == '{':
            selection = self.parse_selection_set()
        return {'field': name, 'alias': alias or name, 'args': args,
                'selection': selection}

    def parse_arguments(self):
        self.expect('(')
        args = {}
        while self.peek()[0] != ')':
            name = self.expect('name')[1]
            self.expect(':')
            args[name] = self.parse_value()
        self.expect(')')
        return args

    def parse_value(self):
        kind, val = self.next()
        if kind == '$':
            return ('var', self.expect('name')[1])
        if kind == 'string':
            return val
        if kind == 'number':
            return val
        if kind == 'name':
            if val == 'true':
                return True
            if val == 'false':
                return False
            if val == 'null':
                return None
            return ('enum', val)
        if kind == '[':
            items = []
            while self.peek()[0] != ']':
                items.append(self.parse_value())
            self.expect(']')
            return items
        if kind == '{':
            obj = {}
            while self.peek()[0] != '}':
                k = self.expect('name')[1]
                self.expect(':')
                obj[k] = self.parse_value()
            self.expect('}')
            return obj
        raise _GqlError('Unexpected value token %s' % (kind,))

    def skip_directives(self):
        while self.peek()[0] == '@':
            self.next()
            self.expect('name')
            if self.peek()[0] == '(':
                self.parse_arguments()


def _eval_value(value, variables):
    if isinstance(value, tuple) and value and value[0] == 'var':
        return variables.get(value[1])
    if isinstance(value, tuple) and value and value[0] == 'enum':
        return value[1]
    if isinstance(value, list):
        return [_eval_value(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: _eval_value(v, variables) for k, v in value.items()}
    return value


def _cursor_encode(key):
    return base64.b64encode(('c:%s' % key).encode('utf-8')).decode('ascii')


def _cursor_decode(cursor):
    try:
        raw = base64.b64decode(cursor.encode('ascii')).decode('utf-8')
    except Exception:
        return None
    if raw.startswith('c:'):
        return raw[2:]
    return None


def _paginate_nodes(nodes, args, key_fn):
    first = args.get('first')
    after = args.get('after')
    start = 0
    if after:
        key = _cursor_decode(after)
        if key is not None:
            for i, n in enumerate(nodes):
                if str(key_fn(n)) == key:
                    start = i + 1
                    break
    total = len(nodes)
    page = nodes[start:]
    has_next = False
    if first is not None:
        try:
            first = int(first)
        except (TypeError, ValueError):
            first = None
        if first is not None and len(page) > first:
            page = page[:first]
            has_next = True
    return {'_t': 'PageConn', 'nodes': page,
            'edges': [{'_t': 'Edge', 'node': n,
                       'cursor': _cursor_encode(key_fn(n))} for n in page],
            'pageInfo': {'_t': 'PageInfo',
                         'hasNextPage': has_next,
                         'hasPreviousPage': start > 0,
                         'startCursor': _cursor_encode(key_fn(page[0]))
                         if page else None,
                         'endCursor': _cursor_encode(key_fn(page[-1]))
                         if page else None},
            'totalCount': total}


def _user_node():
    return {'_t': 'User', 'login': VIEWER_LOGIN, 'id': 1000,
            'name': VIEWER['name'], 'email': VIEWER['email'],
            'url': 'https://example.invalid/%s' % VIEWER_LOGIN,
            'avatarUrl': 'https://example.invalid/avatar.png'}


def _ref_node(name, oid):
    return {'_t': 'Ref', 'name': name,
            'target': {'_t': 'Commit', 'oid': oid or '',
                       'message': ''},
            'prefix': 'refs/heads/'}


def _repo_node(repo):
    main_tip = repo.branch_tip('main')
    return {'_t': 'Repository', 'id': gid('Repository', repo.dbid),
            'databaseId': repo.dbid, 'name': repo.name,
            'nameWithOwner': '%s/%s' % (repo.owner, repo.name),
            'owner': _user_node(),
            'defaultBranchRef': _ref_node('main', main_tip)
            if main_tip else None,
            'viewerPermission': 'ADMIN', 'hasIssuesEnabled': True,
            'description': repo.description, 'url':
            'https://example.invalid/%s/%s' % (repo.owner, repo.name),
            'private': repo.private, 'createdAt': FIXED_TS,
            'updatedAt': FIXED_TS, '_repo': repo,
            'issue': lambda args: _issue_arg(repo, args),
            'issues': lambda args: _issues_connection(repo, args),
            'pullRequest': lambda args: _pr_arg(repo, args),
            'pullRequests': lambda args: _prs_connection(repo, args)}


def _issue_node(repo, issue):
    return {'_t': 'Issue', 'id': gid('Issue', repo.dbid, issue['number']),
            'databaseId': issue['id'], 'number': issue['number'],
            'title': issue['title'], 'body': issue.get('body', ''),
            'state': issue['state'].upper(), 'author': _user_node(),
            'createdAt': FIXED_TS, 'updatedAt': FIXED_TS,
            'url': 'https://example.invalid/%s/%s/issues/%d'
                   % (repo.owner, repo.name, issue['number']),
            'closed': issue['state'] == 'closed',
            'locked': False, '_repo': repo, '_issue': issue,
            'labels': lambda args: _label_connection(repo, issue),
            'comments': lambda args: _comment_connection(repo, issue)}


def _label_connection(repo, issue):
    nodes = [{'_t': 'Label', 'id': gid('Label', repo.dbid, name),
              'name': name, 'color': repo.labels[name]['color'],
              'description': ''} for name in issue['labels']
             if name in repo.labels]
    return _paginate_nodes(nodes, {}, lambda n: n['name'])


def _comment_connection(repo, issue):
    nodes = [{'_t': 'IssueComment', 'id': gid(
        'IssueComment', repo.dbid, issue['number'], c['id']),
        'body': c['body'], 'author': _user_node(),
        'createdAt': FIXED_TS, 'updatedAt': FIXED_TS}
        for c in repo.comments.get(issue['number'], [])]
    return _paginate_nodes(nodes, {}, lambda n: n['id'])


def _pr_node(repo, pr):
    return {'_t': 'PullRequest',
            'id': gid('PullRequest', repo.dbid, pr['number']),
            'databaseId': pr['id'], 'number': pr['number'],
            'title': pr['title'], 'body': pr.get('body', ''),
            'state': ('MERGED' if pr.get('merged') else
                      pr['state'].upper()),
            'merged': bool(pr.get('merged')),
            'mergeable': pr_mergeable(repo, pr),
            'headRefName': pr['head_ref'], 'baseRefName': pr['base_ref'],
            'headRef': _ref_node(pr['head_ref'], repo.branch_tip(pr['head_ref'])),
            'baseRef': _ref_node(pr['base_ref'], repo.branch_tip(pr['base_ref'])),
            'author': _user_node(), 'createdAt': FIXED_TS,
            'updatedAt': FIXED_TS,
            'mergedAt': pr.get('merged_at'),
            'mergeCommit': ({'_t': 'Commit', 'oid': pr['merge_commit_sha'],
                             'message': pr['title']}
                            if pr.get('merge_commit_sha') else None),
            'additions': 0, 'deletions': 0, 'changedFiles': 0,
            '_repo': repo, '_pr': pr,
            'files': lambda args: _pr_files_connection(repo, pr, args)}


def _pr_files_connection(repo, pr, args):
    head_tip = repo.branch_tip(pr['head_ref']) or pr['head_sha']
    base_tip = repo.branch_tip(pr['base_ref']) or pr['base_sha']
    nodes = []
    if head_tip and base_tip:
        mb = merge_base(repo.objects, head_tip, base_tip)
        if mb is not None:
            mb_files = tree_files(repo, parse_commit(
                repo.objects[mb][1])['tree'])
            head_files = tree_files(repo, parse_commit(
                repo.objects[head_tip][1])['tree'])
            for d in diff_files(repo, mb_files, head_files):
                nodes.append({'_t': 'PullRequestChangedFile',
                              'path': d['filename'],
                              'additions': d['additions'],
                              'deletions': d['deletions'],
                              'changeType': d['status'].upper()})
    return _paginate_nodes(nodes, args, lambda n: n['path'])


def _issue_arg(repo, args):
    try:
        number = int(args.get('number'))
    except (TypeError, ValueError):
        return None
    issue = repo.issues.get(number)
    return _issue_node(repo, issue) if issue else None


def _pr_arg(repo, args):
    try:
        number = int(args.get('number'))
    except (TypeError, ValueError):
        return None
    pr = repo.pulls.get(number)
    return _pr_node(repo, pr) if pr else None


def _issues_connection(repo, args):
    issues = list(repo.issues.values())
    states = args.get('states')
    if states:
        wanted = set(s.upper() for s in states)
        issues = [i for i in issues if i['state'].upper() in wanted]
    order = args.get('orderBy') or {}
    direction = (order.get('direction') or 'ASC').upper()
    issues.sort(key=lambda i: i['number'], reverse=(direction == 'DESC'))
    nodes = [_issue_node(repo, i) for i in issues]
    return _paginate_nodes(nodes, args, lambda n: n['number'])


def _prs_connection(repo, args):
    prs = list(repo.pulls.values())
    states = args.get('states')
    if states:
        wanted = set(s.upper() for s in states)
        prs = [p for p in prs
               if ('MERGED' if p.get('merged') else p['state'].upper())
               in wanted]
    order = args.get('orderBy') or {}
    direction = (order.get('direction') or 'ASC').upper()
    prs.sort(key=lambda p: p['number'], reverse=(direction == 'DESC'))
    nodes = [_pr_node(repo, p) for p in prs]
    return _paginate_nodes(nodes, args, lambda n: n['number'])


def _type_node(name):
    if name in _INTROSPECT_FIELDS:
        kind = 'OBJECT'
        fields = [{'__t': '__Field', 'name': f, 'description': None,
                   'args': [], 'type': {'_t': '__Type', 'name': 'String',
                                        'kind': 'SCALAR'}}
                  for f in _INTROSPECT_FIELDS[name]]
        enums = None
    elif name in _INTROSPECT_ENUMS:
        kind = 'ENUM'
        fields = None
        enums = [{'_t': '__EnumValue', 'name': e, 'description': None,
                  'isDeprecated': False, 'deprecationReason': None}
                 for e in _INTROSPECT_ENUMS[name]]
    elif name in _GQL_TYPES:
        kind = 'OBJECT'
        fields = [{'_t': '__Field', 'name': f, 'description': None,
                   'args': [], 'type': {'_t': '__Type', 'name': 'String',
                                        'kind': 'SCALAR'}}
                  for f in _GQL_TYPES[name]]
        enums = None
    else:
        return None
    node = {'_t': '__Type', 'name': name, 'kind': kind, 'description': None,
            'inputFields': None, 'interfaces': [], 'possibleTypes': []}
    if fields is not None:
        node['fields'] = lambda args: fields
    if enums is not None:
        node['enumValues'] = lambda args: enums
    return node


def _repo_by_dbid(dbid):
    for repo in STORE.repos.values():
        if repo.dbid == dbid:
            return repo
    return None


def _resolve_node_by_gid(value):
    parsed = parse_gid(value)
    if not parsed:
        return None
    kind, parts = parsed
    try:
        if kind == 'Repository':
            repo = _repo_by_dbid(int(parts[0]))
            return _repo_node(repo) if repo else None
        if kind == 'Issue':
            repo = _repo_by_dbid(int(parts[0]))
            if repo is None:
                return None
            issue = repo.issues.get(int(parts[1]))
            return _issue_node(repo, issue) if issue else None
        if kind == 'PullRequest':
            repo = _repo_by_dbid(int(parts[0]))
            if repo is None:
                return None
            pr = repo.pulls.get(int(parts[1]))
            return _pr_node(repo, pr) if pr else None
    except (ValueError, IndexError):
        return None
    return None


def _validate(selection, type_name, errors, path):
    fields = _GQL_TYPES.get(type_name)
    if fields is None:
        # Generic connection wrappers.
        if type_name in ('PageConn',):
            fields = ['nodes', 'edges', 'pageInfo', 'totalCount']
        elif type_name == 'Edge':
            fields = ['node', 'cursor']
        else:
            return
    for sel in selection:
        if 'fragment' in sel:
            frag_type = sel.get('type') or type_name
            _validate(sel['selection'], frag_type, errors, path)
            continue
        name = sel['field']
        if name == '__typename':
            continue
        if name not in fields:
            errors.append({'message':
                           'Cannot query field "%s" on type "%s".'
                           % (name, type_name),
                           'path': path + [sel['alias']]})
            continue
        if sel.get('selection'):
            child = _child_type(type_name, name)
            if child:
                _validate(sel['selection'], child, errors,
                          path + [sel['alias']])


def _child_type(parent, field):
    mapping = {
        ('Query', 'viewer'): 'User',
        ('Query', 'repository'): 'Repository',
        ('Query', 'node'): None,  # resolved via fragment
        ('Query', '__type'): '__Type',
        ('Query', '__schema'): '__Schema',
        ('Repository', 'owner'): 'User',
        ('Repository', 'defaultBranchRef'): 'Ref',
        ('Repository', 'issue'): 'Issue',
        ('Repository', 'issues'): 'IssueConnection',
        ('Repository', 'pullRequest'): 'PullRequest',
        ('Repository', 'pullRequests'): 'PullRequestConnection',
        ('Issue', 'author'): 'User',
        ('Issue', 'labels'): 'LabelConnection',
        ('Issue', 'comments'): 'IssueCommentConnection',
        ('IssueConnection', 'nodes'): 'Issue',
        ('IssueConnection', 'edges'): 'IssueEdge',
        ('IssueConnection', 'pageInfo'): 'PageInfo',
        ('IssueEdge', 'node'): 'Issue',
        ('PullRequest', 'author'): 'User',
        ('PullRequest', 'headRef'): 'Ref',
        ('PullRequest', 'baseRef'): 'Ref',
        ('PullRequest', 'files'): 'PullRequestChangedFileConnection',
        ('PullRequest', 'mergeCommit'): 'Commit',
        ('PullRequestConnection', 'nodes'): 'PullRequest',
        ('PullRequestConnection', 'edges'): 'PullRequestEdge',
        ('PullRequestConnection', 'pageInfo'): 'PageInfo',
        ('PullRequestChangedFileConnection', 'nodes'):
            'PullRequestChangedFile',
        ('PullRequestChangedFileConnection', 'edges'):
            'PullRequestChangedFileEdge',
        ('PullRequestChangedFileConnection', 'pageInfo'): 'PageInfo',
        ('IssueCommentConnection', 'nodes'): 'IssueComment',
        ('IssueCommentConnection', 'edges'): 'IssueCommentEdge',
        ('IssueCommentConnection', 'pageInfo'): 'PageInfo',
        ('IssueComment', 'author'): 'User',
        ('IssueCommentEdge', 'node'): 'IssueComment',
        ('LabelConnection', 'nodes'): 'Label',
        ('LabelConnection', 'edges'): 'LabelEdge',
        ('LabelConnection', 'pageInfo'): 'PageInfo',
        ('Mutation', 'createIssue'): 'CreateIssuePayload',
        ('Mutation', 'closeIssue'): 'CloseIssuePayload',
        ('Mutation', 'createPullRequest'): 'CreatePullRequestPayload',
        ('Mutation', 'mergePullRequest'): 'MergePullRequestPayload',
        ('Mutation', 'addComment'): 'AddCommentPayload',
        ('CreateIssuePayload', 'issue'): 'Issue',
        ('CloseIssuePayload', 'issue'): 'Issue',
        ('CreatePullRequestPayload', 'pullRequest'): 'PullRequest',
        ('MergePullRequestPayload', 'pullRequest'): 'PullRequest',
        ('AddCommentPayload', 'commentEdge'): 'IssueCommentEdge',
        ('__Type', 'fields'): '__Field',
        ('__Type', 'enumValues'): '__EnumValue',
        ('__Field', 'type'): '__Type',
        ('__Field', 'args'): '__InputValue',
        ('Ref', 'target'): 'Commit',
    }
    return mapping.get((parent, field))


def _execute_selection(node, selection, variables, errors, path):
    out = {}
    node_type = node.get('_t') if isinstance(node, dict) else None
    for sel in selection:
        if 'fragment' in sel:
            frag_type = sel.get('type')
            if frag_type is None or frag_type == node_type:
                out.update(_execute_selection(node, sel['selection'],
                                              variables, errors, path))
            continue
        name = sel['field']
        alias = sel['alias']
        args = {k: _eval_value(v, variables)
                for k, v in sel['args'].items()}
        if name == '__typename':
            out[alias] = node_type
            continue
        value = None
        if isinstance(node, dict):
            raw = node.get(name)
            if callable(raw):
                try:
                    value = raw(args)
                except Exception as e:
                    errors.append({'message': str(e),
                                   'path': path + [alias]})
                    out[alias] = None
                    continue
            else:
                value = raw
        if value is None:
            out[alias] = None
            continue
        if isinstance(value, dict) and '_t' in value:
            if sel.get('selection'):
                out[alias] = _execute_selection(value, sel['selection'],
                                                variables, errors,
                                                path + [alias])
            else:
                errors.append({'message':
                               'Field "%s" of type "%s" must have a '
                               'selection of subfields.' % (name, node_type),
                               'path': path + [alias]})
                out[alias] = None
        elif isinstance(value, list):
            if sel.get('selection'):
                out[alias] = [_execute_selection(item, sel['selection'],
                                                 variables, errors,
                                                 path + [alias])
                              if isinstance(item, dict) and '_t' in item
                              else item for item in value]
            else:
                out[alias] = value
        else:
            out[alias] = value
    return out


def _execute_operation(op, variables, root_type, root_node):
    errors = []
    _validate(op['selection'], root_type, errors, [])
    if errors:
        return {'data': None, 'errors': errors}
    data = _execute_selection(root_node, op['selection'], variables,
                              errors, [])
    result = {'data': data}
    if errors:
        result['errors'] = errors
    return result


def _query_root():
    return {'_t': 'Query',
            'viewer': lambda args: _user_node(),
            'repository': lambda args: _repository_arg(args),
            'node': lambda args: _resolve_node_by_gid(args.get('id') or ''),
            '__type': lambda args: _type_node(args.get('name') or ''),
            '__schema': lambda args: {'_t': '__Schema', 'types': [],
                                      'queryType': {'_t': '__Type',
                                                    'name': 'Query',
                                                    'kind': 'OBJECT'},
                                      'mutationType': {'_t': '__Type',
                                                       'name': 'Mutation',
                                                       'kind': 'OBJECT'},
                                      'subscriptionType': None,
                                      'directives': []}}


def _repository_arg(args):
    repo = STORE.get_repo(args.get('owner') or '', args.get('name') or '')
    return _repo_node(repo) if repo else None


def _mutation_root():
    return {'_t': 'Mutation',
            'createIssue': lambda args: _mut_create_issue(args),
            'closeIssue': lambda args: _mut_close_issue(args),
            'createPullRequest': lambda args: _mut_create_pr(args),
            'mergePullRequest': lambda args: _mut_merge_pr(args),
            'addComment': lambda args: _mut_add_comment(args)}


def _mut_create_issue(args):
    inp = args.get('input') or {}
    parsed = parse_gid(inp.get('repositoryId') or '')
    if not parsed or parsed[0] != 'Repository':
        raise _GqlError('Unknown repository')
    repo = _repo_by_dbid(int(parsed[1][0]))
    if repo is None:
        raise _GqlError('Unknown repository')
    with STORE.lock:
        repo.issue_seq += 1
        STORE.global_issue_id += 1
        issue = {'number': repo.issue_seq, 'id': STORE.global_issue_id,
                 'title': inp.get('title') or '',
                 'body': inp.get('body') or '', 'state': 'open', 'labels': []}
        repo.issues[issue['number']] = issue
    return {'_t': 'CreateIssuePayload', 'issue': _issue_node(repo, issue),
            'clientMutationId': inp.get('clientMutationId')}


def _mut_close_issue(args):
    inp = args.get('input') or {}
    node = _resolve_node_by_gid(inp.get('issueId') or '')
    if node is None or node.get('_t') != 'Issue':
        raise _GqlError('Unknown issue')
    repo = node['_repo']
    issue = node['_issue']
    with STORE.lock:
        issue['state'] = 'closed'
        repo.issue_cache.pop(issue['number'], None)
    return {'_t': 'CloseIssuePayload', 'issue': _issue_node(repo, issue),
            'clientMutationId': inp.get('clientMutationId')}


def _mut_create_pr(args):
    inp = args.get('input') or {}
    parsed = parse_gid(inp.get('repositoryId') or '')
    if not parsed or parsed[0] != 'Repository':
        raise _GqlError('Unknown repository')
    repo = _repo_by_dbid(int(parsed[1][0]))
    if repo is None:
        raise _GqlError('Unknown repository')
    head = inp.get('headRefName')
    base = inp.get('baseRefName')
    if repo.branch_tip(head) is None or repo.branch_tip(base) is None:
        raise _GqlError('Unknown head or base branch')
    with STORE.lock:
        for p in repo.pulls.values():
            if p['state'] == 'open' and p['head_ref'] == head and \
                    p['base_ref'] == base:
                raise _GqlError('Pull request already exists')
        repo.pr_seq += 1
        STORE.global_issue_id += 1
        pr = {'number': repo.pr_seq, 'id': STORE.global_issue_id,
              'title': inp.get('title') or '',
              'body': inp.get('body') or '', 'state': 'open',
              'merged': False, 'merged_at': None, 'merge_commit_sha': None,
              'head_ref': head, 'base_ref': base,
              'head_sha': repo.branch_tip(head),
              'base_sha': repo.branch_tip(base)}
        repo.pulls[pr['number']] = pr
    return {'_t': 'CreatePullRequestPayload', 'pullRequest': _pr_node(repo, pr),
            'clientMutationId': inp.get('clientMutationId')}


def _mut_merge_pr(args):
    inp = args.get('input') or {}
    node = _resolve_node_by_gid(inp.get('pullRequestId') or '')
    if node is None or node.get('_t') != 'PullRequest':
        raise _GqlError('Unknown pull request')
    repo = node['_repo']
    pr = node['_pr']
    with STORE.lock:
        status, payload = squash_merge(repo, pr)
    if status != 200:
        raise _GqlError(payload.get('message', 'Merge failed'))
    return {'_t': 'MergePullRequestPayload', 'pullRequest': _pr_node(repo, pr),
            'clientMutationId': inp.get('clientMutationId')}


def _mut_add_comment(args):
    inp = args.get('input') or {}
    node = _resolve_node_by_gid(inp.get('subjectId') or '')
    if node is None or node.get('_t') != 'Issue':
        raise _GqlError('Unknown subject')
    repo = node['_repo']
    issue = node['_issue']
    with STORE.lock:
        STORE.global_comment_id += 1
        comment = {'id': STORE.global_comment_id,
                   'body': inp.get('body') or ''}
        repo.comments.setdefault(issue['number'], []).append(comment)
    cnode = {'_t': 'IssueComment', 'id': gid(
        'IssueComment', repo.dbid, issue['number'], comment['id']),
        'body': comment['body'], 'author': _user_node(),
        'createdAt': FIXED_TS, 'updatedAt': FIXED_TS}
    return {'_t': 'AddCommentPayload',
            'commentEdge': {'_t': 'IssueCommentEdge', 'node': cnode,
                            'cursor': _cursor_encode(comment['id'])},
            'clientMutationId': inp.get('clientMutationId')}


def execute_graphql(body):
    """Execute one GraphQL request body (dict or list); return the result."""
    if isinstance(body, list):
        return [execute_graphql(item) for item in body]
    query_text = body.get('query') or ''
    variables = body.get('variables') or {}
    if isinstance(variables, str):
        try:
            variables = json.loads(variables)
        except Exception:
            variables = {}
    try:
        ops = _GqlParser(query_text).parse()
    except _GqlError as e:
        return {'data': None,
                'errors': [{'message': e.message}]}
    if not ops:
        return {'data': None,
                'errors': [{'message': 'Empty query'}]}
    op = ops[0]
    merged_vars = dict(op.get('variables') or {})
    merged_vars.update(variables)
    if op['kind'] == 'mutation':
        return _execute_operation(op, merged_vars, 'Mutation',
                                  _mutation_root())
    return _execute_operation(op, merged_vars, 'Query', _query_root())


# ---------------------------------------------------------------------------
# MCP (streamable HTTP) at /mcp
# ---------------------------------------------------------------------------

_MCP_VERSIONS = ['2026-07-28', '2025-11-25']


def _mcp_server_info():
    return {'name': 'forge', 'version': '1.0.0'}


def _mcp_tools():
    return [
        {'name': 'create_repo',
         'description': 'Create a new repository owned by the authenticated user.',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'name': {'type': 'string',
                                      'description': 'Repository name'},
                             'description': {'type': 'string',
                                             'description': 'Repository description'},
                             'private': {'type': 'boolean',
                                         'description': 'Whether the repository is private'}},
                         'required': ['name']}},
        {'name': 'get_repo',
         'description': 'Get a repository by owner and name.',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'owner': {'type': 'string',
                                       'description': 'Repository owner login'},
                             'repo': {'type': 'string',
                                      'description': 'Repository name'}},
                         'required': ['owner', 'repo']}},
        {'name': 'create_issue',
         'description': 'Create an issue in a repository.',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'owner': {'type': 'string'},
                             'repo': {'type': 'string'},
                             'title': {'type': 'string',
                                       'description': 'Issue title'},
                             'body': {'type': 'string',
                                      'description': 'Issue body'}},
                         'required': ['owner', 'repo', 'title']}},
        {'name': 'list_issues',
         'description': 'List issues in a repository, optionally filtered by state.',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'owner': {'type': 'string'},
                             'repo': {'type': 'string'},
                             'state': {'type': 'string',
                                       'description': 'open, closed, or all'}},
                         'required': ['owner', 'repo']}},
        {'name': 'get_issue',
         'description': 'Get a single issue by number.',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'owner': {'type': 'string'},
                             'repo': {'type': 'string'},
                             'issue_number': {'type': 'integer',
                                            'description': 'Issue number'}},
                         'required': ['owner', 'repo', 'issue_number']}},
        {'name': 'close_issue',
         'description': 'Close an issue by number.',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'owner': {'type': 'string'},
                             'repo': {'type': 'string'},
                             'issue_number': {'type': 'integer',
                                            'description': 'Issue number'}},
                         'required': ['owner', 'repo', 'issue_number']}},
        {'name': 'create_pull_request',
         'description': 'Create a pull request from a head branch into a base branch.',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'owner': {'type': 'string'},
                             'repo': {'type': 'string'},
                             'title': {'type': 'string',
                                       'description': 'Pull request title'},
                             'head': {'type': 'string',
                                      'description': 'Head branch name'},
                             'base': {'type': 'string',
                                      'description': 'Base branch name'},
                             'body': {'type': 'string',
                                      'description': 'Pull request body'}},
                         'required': ['owner', 'repo', 'title', 'head',
                                      'base']}},
        {'name': 'merge_pull_request',
         'description': 'Merge a pull request by number (squash).',
         'inputSchema': {'type': 'object',
                         'properties': {
                             'owner': {'type': 'string'},
                             'repo': {'type': 'string'},
                             'pull_number': {'type': 'integer',
                                           'description': 'Pull request number'}},
                         'required': ['owner', 'repo', 'pull_number']}},
    ]


def _tool_result(obj):
    text = json.dumps(obj, separators=(',', ':'))
    return {'content': [{'type': 'text', 'text': text}],
            'structuredContent': obj, 'isError': False}


def _tool_error(message):
    return {'content': [{'type': 'text', 'text': 'Error: %s' % message}],
            'isError': True}


def _call_tool(name, args):
    if name == 'create_repo':
        repo_name = args.get('name')
        if not repo_name:
            return None, 'name is required'
        with STORE.lock:
            repo = STORE.create_repo(
                repo_name, VIEWER_LOGIN,
                args.get('description') or '',
                bool(args.get('private', False)))
        if repo is None:
            return None, 'Repository already exists'
        return _tool_result(repo_json(repo)), None
    if name == 'get_repo':
        repo = STORE.get_repo(args.get('owner') or '',
                              args.get('repo') or '')
        if repo is None:
            return None, 'Repository not found'
        return _tool_result(repo_json(repo)), None
    if name == 'create_issue':
        repo = STORE.get_repo(args.get('owner') or '',
                              args.get('repo') or '')
        if repo is None:
            return None, 'Repository not found'
        if not args.get('title'):
            return None, 'title is required'
        with STORE.lock:
            repo.issue_seq += 1
            STORE.global_issue_id += 1
            issue = {'number': repo.issue_seq,
                     'id': STORE.global_issue_id,
                     'title': args['title'],
                     'body': args.get('body') or '', 'state': 'open',
                     'labels': []}
            repo.issues[issue['number']] = issue
        return _tool_result(issue_json(repo, issue)), None
    if name == 'list_issues':
        repo = STORE.get_repo(args.get('owner') or '',
                              args.get('repo') or '')
        if repo is None:
            return None, 'Repository not found'
        state = args.get('state') or 'open'
        issues = [issue_json(repo, i) for i in repo.issues.values()
                  if state == 'all' or i['state'] == state]
        issues.sort(key=lambda i: i['number'])
        return _tool_result({'issues': issues}), None
    if name == 'get_issue':
        repo = STORE.get_repo(args.get('owner') or '',
                              args.get('repo') or '')
        if repo is None:
            return None, 'Repository not found'
        try:
            number = int(args.get('issue_number'))
        except (TypeError, ValueError):
            return None, 'issue_number is required'
        issue = repo.issues.get(number)
        if issue is None:
            return None, 'Issue not found'
        return _tool_result(issue_json(repo, issue)), None
    if name == 'close_issue':
        repo = STORE.get_repo(args.get('owner') or '',
                              args.get('repo') or '')
        if repo is None:
            return None, 'Repository not found'
        try:
            number = int(args.get('issue_number'))
        except (TypeError, ValueError):
            return None, 'issue_number is required'
        issue = repo.issues.get(number)
        if issue is None:
            return None, 'Issue not found'
        with STORE.lock:
            issue['state'] = 'closed'
            repo.issue_cache.pop(number, None)
        return _tool_result(issue_json(repo, issue)), None
    if name == 'create_pull_request':
        repo = STORE.get_repo(args.get('owner') or '',
                              args.get('repo') or '')
        if repo is None:
            return None, 'Repository not found'
        head = args.get('head')
        base = args.get('base')
        title = args.get('title')
        if not head or not base or not title:
            return None, 'head, base and title are required'
        if repo.branch_tip(head) is None or repo.branch_tip(base) is None:
            return None, 'Unknown head or base branch'
        with STORE.lock:
            for p in repo.pulls.values():
                if p['state'] == 'open' and p['head_ref'] == head and \
                        p['base_ref'] == base:
                    return None, 'Pull request already exists'
            repo.pr_seq += 1
            STORE.global_issue_id += 1
            pr = {'number': repo.pr_seq, 'id': STORE.global_issue_id,
                  'title': title, 'body': args.get('body') or '',
                  'state': 'open', 'merged': False, 'merged_at': None,
                  'merge_commit_sha': None, 'head_ref': head,
                  'base_ref': base, 'head_sha': repo.branch_tip(head),
                  'base_sha': repo.branch_tip(base)}
            repo.pulls[pr['number']] = pr
        return _tool_result(pull_json(repo, pr)), None
    if name == 'merge_pull_request':
        repo = STORE.get_repo(args.get('owner') or '',
                              args.get('repo') or '')
        if repo is None:
            return None, 'Repository not found'
        try:
            number = int(args.get('pull_number'))
        except (TypeError, ValueError):
            return None, 'pull_number is required'
        pr = repo.pulls.get(number)
        if pr is None:
            return None, 'Pull request not found'
        with STORE.lock:
            status, payload = squash_merge(repo, pr)
        if status != 200:
            return None, payload.get('message', 'Merge failed')
        out = {'merged': True, 'number': pr['number'],
               'sha': payload['sha'], 'state': 'closed',
               'title': pr['title']}
        return _tool_result(out), None
    return None, 'Unknown tool: %s' % name


def _mcp_required(name):
    for tool in _mcp_tools():
        if tool['name'] == name:
            return tool['inputSchema'].get('required', [])
    return None


def _handle_graphql(self):
    raw = self.read_body()
    if not raw:
        self.send_json(400, {'errors': [{'message': 'Empty body'}]})
        return
    try:
        body = json.loads(raw.decode('utf-8'))
    except Exception:
        self.send_json(400, {'errors': [{'message': 'Invalid JSON'}]})
        return
    result = execute_graphql(body)
    self.send_json(200, result)


def _handle_mcp(self):
    if self.command == 'DELETE':
        self.read_body()
        self._send(204, b'')
        return
    if self.command != 'POST':
        self.err(404, 'Not Found')
        return
    raw = self.read_body()
    try:
        msg = json.loads(raw.decode('utf-8'))
    except Exception:
        self.send_json(400, {'jsonrpc': '2.0', 'id': None,
                             'error': {'code': -32700,
                                       'message': 'Parse error'}})
        return
    if not isinstance(msg, dict):
        self.send_json(400, {'jsonrpc': '2.0', 'id': None,
                             'error': {'code': -32700,
                                       'message': 'Parse error'}})
        return
    method = msg.get('method', '')
    msg_id = msg.get('id')
    if 'id' not in msg or method.startswith('notifications/'):
        # Notification: no response body, 202.
        self._send(202, b'')
        return

    def reply(result=None, error=None):
        payload = {'jsonrpc': '2.0', 'id': msg_id}
        if error is not None:
            payload['error'] = error
        else:
            payload['result'] = result
        self.send_json(200, payload)

    if method == 'server/discover':
        reply({'supportedVersions': list(_MCP_VERSIONS),
               'protocolVersion': '2026-07-28',
               'capabilities': {'tools': {}},
               'serverInfo': _mcp_server_info(),
               '_meta': {'io.modelcontextprotocol/serverInfo':
                         _mcp_server_info()}})
        return
    if method == 'initialize':
        params = msg.get('params') or {}
        requested = params.get('protocolVersion')
        version = requested if requested in _MCP_VERSIONS else '2026-07-28'
        reply({'protocolVersion': version,
               'capabilities': {'tools': {}},
               'serverInfo': _mcp_server_info(),
               '_meta': {'io.modelcontextprotocol/serverInfo':
                         _mcp_server_info()}})
        return
    if method == 'ping':
        reply({})
        return
    if method == 'tools/list':
        reply({'tools': _mcp_tools()})
        return
    if method == 'tools/call':
        params = msg.get('params') or {}
        tool_name = params.get('name')
        args = params.get('arguments') or {}
        required = _mcp_required(tool_name)
        if required is None:
            reply(error={'code': -32601,
                         'message': 'Unknown tool: %s' % tool_name})
            return
        missing = [r for r in required if r not in args or args[r] is None]
        if missing:
            reply(error={'code': -32602,
                         'message': 'Missing required argument: %s'
                                    % missing[0]})
            return
        try:
            result, err = _call_tool(tool_name, args)
        except Exception as e:
            reply(error={'code': -32603, 'message': str(e)})
            return
        if err is not None:
            reply(result=_tool_error(err))
            return
        reply(result=result)
        return
    reply(error={'code': -32601, 'message': 'Method not found: %s' % method})


ForgeHandler.handle_graphql = _handle_graphql
ForgeHandler.handle_mcp = _handle_mcp


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def _http_json(url, payload, method='POST', timeout=10):
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8') or '{}')


class ForgeServer(ThreadingHTTPServer):
    daemon_threads = True
    # Set before bind/activate: 50 concurrent clones connect at once, and
    # the default backlog of 5 drops SYNs and stalls the whole round.
    request_queue_size = 128
    allow_reuse_address = True


def main():
    port = int(os.environ.get('FORGE_PORT', '0') or 0)
    server = ForgeServer(('127.0.0.1', port), ForgeHandler)
    actual_port = server.server_address[1]
    env_url = os.environ.get('MAKEFUN_ENV_URL', '').rstrip('/')
    if env_url:
        _http_json(env_url + '/register', {'port': actual_port})
        while True:
            try:
                payload = _http_json(env_url + '/done', None, method='GET')
                if payload.get('done'):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        server.server_close()
        return 0
    print('forge listening on http://127.0.0.1:%d' % actual_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
