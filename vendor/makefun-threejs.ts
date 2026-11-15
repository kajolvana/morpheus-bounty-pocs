// make.fun's three.js game library for Node 22: one file, the Node standard library only, erasable
// TypeScript, so it runs as-is under `node main.ts` with no flags, no build and no install. Copy it to
// vendor/makefun-threejs.ts in your repository (a copy anywhere else counts as lines you wrote) and import it:
//
//   import { buildDossier } from './vendor/makefun-threejs.ts';
//   writeFileSync('/output/dossier.json', buildDossier('/output', {
//     text_max_bytes: 163840,
//     screenshots: { max: 4, max_bytes: 262144 },
//     game_max_files: 993,
//     game_max_bytes: 50331648,
//   }));
//
// The bounty's scorer runs this same file, byte for byte, on what you hand in, and requires your
// dossier.json to equal what buildDossier writes from your game/ and screenshots/.
//
//   readGameTree(dir, caps)     every file and directory under game/, by path with its bytes and its class
//                               (source, vendored or asset) and the totals; caps (each optional) bound how
//                               many entries it holds, how many bytes and how long a path may be
//   classifyGamePath(path)      'source', 'vendored' or 'asset', by the bounty's source rule: a file under
//                               game/ with a source extension, not under a directory named vendor and not
//                               named *.min.js/*.min.mjs/*.min.css
//   htmlFacts(html)             a page's <script>, <link>, <img>, <source>, <video>, <audio> and <iframe>
//                               tags and its <script type="importmap"> blocks, in document order, attributes parsed
//   importMapOf(facts)          the page's one import map, parsed (imports and scopes, a null value allowed), or why
//                               it has none
//   isRelativeRef(ref)          whether a reference is relative: no scheme, no leading // and no leading /
//   isRelativeSpecifier(name)   whether a module specifier is relative in JavaScript's sense (./ or ../): a bare
//                               name such as "three" is not, whatever the import map then makes of it
//   resolveGamePath(ref, from)  the path under game/ a relative reference names, from the importing file's
//                               path; null when the reference is not relative or climbs out of game/
//   importMapMatch(map, name, from)
//                               what an import map says for a bare specifier imported from `from`: the scoped or
//                               top-level entry, found false when neither maps it, or value null when a null entry
//                               blocks it
//   mappedSpecifier(map, name, from)
//                               the value an import map gives a bare specifier: an exact key, else the longest key
//                               ending in / that prefixes it, a matching scope's map first; null when none or blocked
//   sourceImports(text)         a module's static import and export-from specifiers, in source order, by a
//                               lexical scan (the gate's own check parses the module, see below)
//   importOrder(sources, facts) the source JS modules in the order a depth-first walk of static imports
//                               reaches them from index.html's module scripts (inline or src), specifiers
//                               resolved relative to the importer or through the import map, unresolvable or
//                               unmapped bare specifiers skipped
//   canonicalJson(value)        JSON with sorted keys and no spaces: one spelling per value
//   buildDossier(dir, caps)     the dossier (makefun/dossier/v1) of an entry directory, as canonical JSON bytes
//
// The bounty's scorer gives buildDossier { text_max_bytes, screenshots: { max, max_bytes }, game_max_files,
// game_max_bytes, game_path_max_bytes } from its gate.json (whose keys are exactly these). Only text_max_bytes
// shapes the bytes; the other caps only refuse, so passing them too makes your program fail where the scorer
// would. The gate's game_max_files is 993, not 1000: the grader caps /output itself at 1000 entries (files and
// directories counted alike, grader/program.ts MAX_OUTPUT_FILES), and game/ counts alongside its entries, with
// screenshots/ and its 4 files and dossier.json, so 993 game entries plus those make 1000.
//
// An entry hands in game/ (the playable game as static files the browser loads: game/index.html required, one
// <script type="importmap"> mapping "three" to the game-relative path of three.js 0.187.0's module, its
// three.core.min.js beside it, and the game's own modules) and screenshots/ (3 or 4 PNG or JPEG renders of the
// game, the first by name the cover). Nothing of either is read over the network.
//
// The dossier's text is a "## Game" block and then the sources: index.html first, then the source JS modules in
// importOrder's depth-first order, then every other source file by path. A source file is one under game/ with
// extension .html, .js, .mjs, .css, .glsl, .vert, .frag, .wgsl or .json, not under a directory named vendor and
// not named *.min.js, *.min.mjs or *.min.css; every other file is listed in the Game block (vendored files by
// path and bytes, assets by extension with counts and bytes), never in the text. Each source follows a header
// line naming its path, lines and bytes and then its text in a fence. The text holds at most caps.text_max_bytes
// UTF-8 bytes: every source that fits goes in whole, in that order; the first that does not goes in up to its last
// complete line that fits (its fence closed, then a line naming how many of its lines are not shown), or not at
// all when not one line fits; nothing follows it but a last line naming how many whole files and how many bytes of
// source were left out. The text is deterministic: the same directory writes the same bytes.
//
// buildDossier throws DossierError on input it refuses: no game/ directory (or not a directory), an entry of
// game/ that is a symbolic link or other special file, game/ past game_max_files entries or game_max_bytes bytes
// or a path past game_path_max_bytes, no game/index.html (or one over 1 MiB), screenshots/ holding no image or
// more than the count cap, an image over its byte cap or neither PNG nor JPEG, and a text cap too small for the
// Game block and the cut line. Its error's `limit` names the cap that refused, when a cap did.
//
// The scorer's references row parses every source module with acorn (its vendored copy), so the static-import scan
// here is lexical only: it follows what a module says it imports for the dossier's order, and its refusals are the
// gate's business, not this file's. The scan skips comments, strings, templates and regular expressions; a specifier
// it cannot make sense of is left to the gate.

import { createHash } from 'node:crypto';
import { lstatSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

/** The dossier schema buildDossier writes (contracts/dossier.ts DOSSIER_SCHEMA). */
export const DOSSIER_SCHEMA = 'makefun/dossier/v1';
/** The most bytes a dossier holds (contracts/dossier.ts DOSSIER_MAX_BYTES): 4 MiB. */
export const DOSSIER_MAX_BYTES = 4 * 1024 * 1024;
/** The most images a dossier holds (contracts/dossier.ts DOSSIER_MAX_IMAGES). */
export const DOSSIER_MAX_IMAGES = 8;
/** The three.js release the kit ships, whose module the import map maps "three" to. */
export const THREE_VERSION = '0.187.0';
/** The names the kit's three.js module and its core sit under, inside game/. */
export const THREE_MODULE_FILE = 'three.module.min.js';
export const THREE_CORE_FILE = 'three.core.min.js';
/** The game's page: the one file game/ must hold. */
export const INDEX_HTML = 'index.html';
/** The most bytes game/index.html may be. */
export const INDEX_HTML_MAX_BYTES = 1024 * 1024;
/** The longest path a game file may have, in UTF-8 bytes: the platform's 1024-byte output path less 'game/'. */
export const GAME_PATH_MAX_BYTES = 1000;
/** How many vendored files the Game block lists by path before it counts the rest. */
const VENDORED_LISTED = 24;

/** The extensions a source file has (lowercase; matched case-insensitively). */
export const SOURCE_EXTENSIONS = ['html', 'js', 'mjs', 'css', 'glsl', 'vert', 'frag', 'wgsl', 'json'] as const;
/** The names a generated file has: not source, whatever its extension. */
const GENERATED_NAME = /\.min\.(?:js|mjs|css)$/i;
/** A source file's extension. */
const SOURCE_NAME = new RegExp(`\\.(?:${SOURCE_EXTENSIONS.join('|')})$`, 'i');
/** A JS module the import walk follows. */
const MODULE_NAME = /\.(?:js|mjs)$/i;

/** What a file under game/ is to the dossier: a source it shows, a vendored file or an asset it only counts. */
export type GameFileClass = 'source' | 'vendored' | 'asset';

/** A cap buildDossier or readGameTree refuses on, named in DossierError.limit. */
export type DossierLimit = 'text_max_bytes' | 'screenshots.max' | 'screenshots.max_bytes' | 'game_max_files' | 'game_max_bytes' | 'game_path_max_bytes';

/** Why buildDossier writes no dossier for a directory: its caps, its game/ or its screenshots. */
export class DossierError extends Error {
  /** The caller's cap the input is past (a key of DossierCaps or GameCaps), or null when the input itself is at fault. */
  readonly limit: DossierLimit | null;
  constructor(message: string, limit: DossierLimit | null = null) {
    super(message);
    this.name = 'DossierError';
    this.limit = limit;
  }
}

/**
 * What readGameTree is held to, each cap absent for none.
 *   game_max_files        the files and directories under game/, summed (one entry each, as the platform counts them).
 *   game_max_bytes        the files' sizes, summed (the bytes game/ costs, its directories costing none).
 *   game_path_max_bytes   each path's length in UTF-8 bytes, relative to game/.
 */
export interface GameCaps {
  game_max_files?: number;
  game_max_bytes?: number;
  game_path_max_bytes?: number;
}

/** One file under game/. */
export interface GameFile {
  /** POSIX path relative to game/, like `src/main.js`. */
  path: string;
  /** Its size in bytes. */
  bytes: number;
  cls: GameFileClass;
}

/** Every entry under game/, walked once. */
export interface GameTree {
  /** The files, in code-unit path order. */
  files: GameFile[];
  /** The directories' paths, in code-unit order (game/ itself is '' and not listed). */
  directories: string[];
  /** How many entries game/ holds: files and directories, the platform's own count. */
  count: number;
  /** The files' sizes, summed. */
  totalBytes: number;
}

/**
 * What buildDossier is held to: the text's cap in UTF-8 bytes, the screenshot caps (at most `max` images, each at
 * most `max_bytes` bytes; the dossier's own DOSSIER_MAX_IMAGES and DOSSIER_MAX_BYTES hold either way) and
 * readGameTree's caps. A bounty's gate.json may be passed whole: buildDossier reads only these keys (threejs-fps-01's
 * gate names them text_max_bytes, screenshots, game_max_files, game_max_bytes and game_path_max_bytes).
 */
export interface DossierCaps {
  text_max_bytes: number;
  screenshots?: { max: number; max_bytes: number };
  game_max_files?: number;
  game_max_bytes?: number;
  game_path_max_bytes?: number;
}

// ---------------------------------------------------------------- bytes

const utf8 = new TextDecoder('utf-8');

/** Code-unit order, the same on every machine (localeCompare is not). */
const byCodeUnit = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

const sha256Hex = (bytes: Uint8Array): string => createHash('sha256').update(bytes).digest('hex');

const byteLength = (s: string): number => Buffer.byteLength(s, 'utf8');

/** A string's bytes as text, for display only (invalid UTF-8 becomes U+FFFD). */
const text = (bytes: Uint8Array): string => utf8.decode(bytes);

/** Lines of a source: 0 when empty, else its newlines plus one for a last line with none. */
function lineCount(bytes: Uint8Array): number {
  if (bytes.length === 0) return 0;
  let n = 0;
  for (const b of bytes) if (b === 0x0a) n++;
  return bytes[bytes.length - 1] === 0x0a ? n : n + 1;
}

/** A Markdown fence one backtick longer than the longest run in `body`, three at least. */
function fenceFor(body: string): string {
  let longest = 0;
  for (const run of body.match(/`+/g) ?? []) longest = Math.max(longest, run.length);
  return '`'.repeat(Math.max(3, longest + 1));
}

/** The fence's language tag for a source path. */
function languageOf(path: string): string {
  const ext = /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase() ?? '';
  if (ext === 'mjs' || ext === 'js') return 'js';
  if (ext === 'glsl' || ext === 'vert' || ext === 'frag') return 'glsl';
  if (ext === 'wgsl') return 'wgsl';
  if (ext === 'html') return 'html';
  if (ext === 'css') return 'css';
  if (ext === 'json') return 'json';
  return 'text';
}

// ---------------------------------------------------------------- the game tree

/** A caller's GameCaps checked: each given cap a whole number, each missing one Infinity. */
function gameCapsOf(caps: GameCaps, who: string): Record<keyof GameCaps, number> {
  const out = { game_max_files: Infinity, game_max_bytes: Infinity, game_path_max_bytes: GAME_PATH_MAX_BYTES };
  for (const k of ['game_max_files', 'game_max_bytes', 'game_path_max_bytes'] as const) {
    const v = caps[k] as unknown;
    if (v === undefined) continue;
    if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < 0) throw new TypeError(`${who}: caps.${k} must be a whole number, not ${String(v)}`);
    out[k] = v;
  }
  return out;
}

/**
 * What a path under game/ is to the dossier: a source it shows, or a vendored file or an asset it only counts.
 * A generated name (*.min.js, *.min.mjs, *.min.css) and any path under a directory named vendor are vendored, a
 * source extension outside them is source, and everything else is an asset.
 */
export function classifyGamePath(path: string): GameFileClass {
  const segs = path.split('/');
  const base = segs[segs.length - 1] ?? '';
  const dirs = segs.slice(0, -1);
  if (dirs.includes('vendor') || GENERATED_NAME.test(base)) return 'vendored';
  return SOURCE_NAME.test(base) ? 'source' : 'asset';
}

/** Whether a path under game/ is a source file (classifyGamePath returns 'source'). */
export function isSourcePath(path: string): boolean {
  return classifyGamePath(path) === 'source';
}

/**
 * Every file and directory under game/, by path, with each file's bytes and class and the totals. Symbolic links
 * and other special files are refused, as are more than caps.game_max_files entries, more than caps.game_max_bytes
 * bytes in the files, and a path longer than caps.game_path_max_bytes. Nothing is read but the directory listing.
 * Throws DossierError.
 */
export function readGameTree(gameDir: string, caps: GameCaps = {}): GameTree {
  const c = gameCapsOf(caps, 'readGameTree');
  let root;
  try {
    root = lstatSync(gameDir);
  } catch {
    throw new DossierError('game/: no such directory: a game is a directory of static files, its index.html at its root');
  }
  if (!root.isDirectory()) throw new DossierError('game/: not a directory (a game is a directory of static files)');
  const files: GameFile[] = [];
  const directories: string[] = [];
  const stack: string[] = [''];
  let count = 0;
  let totalBytes = 0;
  while (stack.length > 0) {
    const rel = stack.pop()!;
    let entries;
    try {
      entries = readdirSync(rel === '' ? gameDir : join(gameDir, rel), { withFileTypes: true });
    } catch (err) {
      throw new DossierError(`game/${rel === '' ? '' : `${rel}/`}: cannot be read (${(err as Error).message})`);
    }
    for (const e of entries) {
      const path = rel === '' ? e.name : `${rel}/${e.name}`;
      const pathBytes = byteLength(path);
      if (pathBytes > c.game_path_max_bytes) {
        throw new DossierError(`game/${path}: a path of ${pathBytes} bytes, over the ${c.game_path_max_bytes} allowed (game_path_max_bytes)`, 'game_path_max_bytes');
      }
      const shown = `game/${path}`;
      let st;
      try {
        st = lstatSync(join(gameDir, path));
      } catch {
        throw new DossierError(`${shown}: cannot be read`);
      }
      count++;
      if (count > c.game_max_files) {
        throw new DossierError(`game/: holds more than the ${c.game_max_files} files and directories allowed (game_max_files)`, 'game_max_files');
      }
      if (st.isDirectory()) {
        directories.push(path);
        stack.push(path);
      } else if (st.isFile()) {
        totalBytes += st.size;
        if (totalBytes > c.game_max_bytes) {
          throw new DossierError(`game/: its files come to more than the ${c.game_max_bytes} bytes allowed (game_max_bytes)`, 'game_max_bytes');
        }
        files.push({ path, bytes: st.size, cls: classifyGamePath(path) });
      } else {
        throw new DossierError(`${shown}: a symbolic link or special file (game/ holds plain files and directories only)`);
      }
    }
  }
  files.sort((a, b) => byCodeUnit(a.path, b.path));
  directories.sort(byCodeUnit);
  return { files, directories, count, totalBytes };
}

/** game/index.html's bytes from the tree, or why there is none: refused when missing, a directory, a link or over the cap. */
function readIndexHtml(gameDir: string, tree: GameTree): Uint8Array {
  const file = tree.files.find((f) => f.path === INDEX_HTML);
  if (file === undefined) {
    if (tree.directories.includes(INDEX_HTML)) throw new DossierError('buildDossier: game/index.html: a directory, not the page');
    throw new DossierError('buildDossier: game/index.html: no such file: a game is a directory of static files, its index.html at its root');
  }
  if (file.bytes > INDEX_HTML_MAX_BYTES) throw new DossierError(`buildDossier: game/index.html: ${file.bytes} bytes, over the ${INDEX_HTML_MAX_BYTES} a page may be`);
  try {
    return new Uint8Array(readFileSync(join(gameDir, INDEX_HTML)));
  } catch (err) {
    throw new DossierError(`buildDossier: game/index.html: cannot be read (${(err as Error).message})`);
  }
}

// ---------------------------------------------------------------- the page

/** One <script> of a page. */
export interface HtmlScript {
  /** Its src attribute, or null when the script is inline. */
  src: string | null;
  /** Its type attribute, lowercased and trimmed ('' when absent). */
  type: string;
  /** An inline script's text ('' for one with src or no body). */
  text: string;
}

/** One <link> of a page. */
export interface HtmlLink {
  href: string;
  /** Its rel attribute, lowercased and trimmed. */
  rel: string;
}

/** One element of a page that loads a file through its src attribute: <img>, <source>, <video>, <audio> or <iframe>. */
export interface HtmlResource {
  /** Its tag name, lowercased. */
  tag: string;
  /** Its src attribute. */
  src: string;
}

/** The tags whose src is a resource the page loads. */
const RESOURCE_TAGS = ['img', 'source', 'video', 'audio', 'iframe'] as const;

/** One <script type="importmap"> block. */
export interface HtmlImportMap {
  /** Its JSON, when it parsed. */
  value: unknown;
  /** Why its text is not JSON, or null. */
  error: string | null;
}

/** What a page's markup says about the files it loads. */
export interface HtmlFacts {
  /** Every <script>, in document order. */
  scripts: HtmlScript[];
  /** Every <link> with an href, in document order. */
  links: HtmlLink[];
  /** Every <img>, <source>, <video>, <audio> and <iframe> with a src, in document order. */
  resources: HtmlResource[];
  /** Every <script type="importmap">, in document order. */
  importMaps: HtmlImportMap[];
}

/** The index of `>` ending a tag whose body starts at `from`, honoring quoted attribute values; -1 when there is none. */
function tagEnd(html: string, from: number): number {
  let quote = '';
  for (let i = from; i < html.length; i++) {
    const ch = html[i]!;
    if (quote !== '') {
      if (ch === quote) quote = '';
      continue;
    }
    if (ch === '"' || ch === "'") quote = ch;
    else if (ch === '>') return i;
  }
  return -1;
}

/** A tag body's attributes, names lowercased, values unquoted; a present attribute with no value is ''. */
function attributesOf(body: string): Map<string, string> {
  const out = new Map<string, string>();
  let i = 0;
  while (i < body.length) {
    while (i < body.length && /[\s/]/.test(body[i]!)) i++;
    if (i >= body.length) break;
    const start = i;
    while (i < body.length && !/[\s=/>]/.test(body[i]!)) i++;
    const name = body.slice(start, i).toLowerCase();
    while (i < body.length && /\s/.test(body[i]!)) i++;
    let value = '';
    if (body[i] === '=') {
      i++;
      while (i < body.length && /\s/.test(body[i]!)) i++;
      const quote = body[i];
      if (quote === '"' || quote === "'") {
        const end = body.indexOf(quote, i + 1);
        value = body.slice(i + 1, end < 0 ? body.length : end);
        i = end < 0 ? body.length : end + 1;
      } else {
        // An unquoted value runs to whitespace or the tag's >; a / inside it is part of the value.
        const s = i;
        while (i < body.length && !/[\s>]/.test(body[i]!)) i++;
        value = body.slice(s, i);
      }
    }
    if (name !== '') out.set(name, value);
  }
  return out;
}

/** The index of `</name` at or after `from`, ASCII case-insensitively; -1 when there is none. */
function closeIndexOf(html: string, from: number, name: string): number {
  const needle = `</${name}`;
  for (let i = from; i + needle.length <= html.length; i++) {
    if (html[i] !== '<') continue;
    let hit = true;
    for (let j = 1; j < needle.length; j++) {
      const a = html[i + j]!.toLowerCase();
      if (a !== needle[j]) {
        hit = false;
        break;
      }
    }
    if (hit) return i;
  }
  return -1;
}

/**
 * Every <script>, <link href>, <img src>, <source src>, <video src>, <audio src>, <iframe src> and
 * <script type="importmap"> of a page, in document order. Comments and the document type are skipped. This is a tag
 * scan, not an HTML parser: it reads the attributes these tags carry and nothing else.
 */
export function htmlFacts(html: string): HtmlFacts {
  const scripts: HtmlScript[] = [];
  const links: HtmlLink[] = [];
  const resources: HtmlResource[] = [];
  const importMaps: HtmlImportMap[] = [];
  let i = 0;
  while (i < html.length) {
    const at = html.indexOf('<', i);
    if (at < 0) break;
    if (html.startsWith('<!--', at)) {
      const end = html.indexOf('-->', at + 4);
      i = end < 0 ? html.length : end + 3;
      continue;
    }
    if (html.startsWith('<!', at)) {
      const end = html.indexOf('>', at + 2);
      i = end < 0 ? html.length : end + 1;
      continue;
    }
    const rest = html.slice(at + 1, at + 8).toLowerCase();
    if (rest.startsWith('script') && !/[A-Za-z0-9]/.test(html[at + 7] ?? '')) {
      const end = tagEnd(html, at + 7);
      if (end < 0) break;
      const attrs = attributesOf(html.slice(at + 7, end));
      const type = (attrs.get('type') ?? '').trim().toLowerCase();
      const src = attrs.get('src') ?? null;
      let body = '';
      if (src === null) {
        const close = closeIndexOf(html, end + 1, 'script');
        body = html.slice(end + 1, close < 0 ? html.length : close);
        i = close < 0 ? html.length : close + 8;
      } else {
        const close = closeIndexOf(html, end + 1, 'script');
        i = close < 0 ? html.length : close + 8;
      }
      if (type === 'importmap') {
        let value: unknown = null;
        let error: string | null = null;
        try {
          value = JSON.parse(body);
        } catch (err) {
          error = (err as Error).message;
        }
        importMaps.push({ value, error });
      } else {
        scripts.push({ src, type, text: src === null ? body : '' });
      }
      continue;
    }
    if (rest.startsWith('link') && !/[A-Za-z0-9]/.test(html[at + 5] ?? '')) {
      const end = tagEnd(html, at + 5);
      if (end < 0) break;
      const attrs = attributesOf(html.slice(at + 5, end));
      const href = attrs.get('href');
      if (href !== undefined) links.push({ href, rel: (attrs.get('rel') ?? '').trim().toLowerCase() });
      i = end + 1;
      continue;
    }
    const tag = RESOURCE_TAGS.find((name) => rest.startsWith(name) && !/[A-Za-z0-9]/.test(html[at + 1 + name.length] ?? ''));
    if (tag !== undefined) {
      const end = tagEnd(html, at + 1 + tag.length);
      if (end < 0) break;
      const attrs = attributesOf(html.slice(at + 1 + tag.length, end));
      const src = attrs.get('src');
      if (src !== undefined) resources.push({ tag, src });
      i = end + 1;
      continue;
    }
    const end = tagEnd(html, at + 1);
    i = end < 0 ? html.length : end + 1;
  }
  return { scripts, links, resources, importMaps };
}

/** A page's import map: its imports and scopes, each a specifier to a reference; a null value blocks its specifier. */
export interface ImportMap {
  imports: Record<string, string | null>;
  scopes: Record<string, Record<string, string | null>>;
}

/** A page's one import map, or why it has none: `map` is null when `error` says why. */
export interface ImportMapRead {
  map: ImportMap | null;
  error: string | null;
}

/** Whether `value` is a plain object. */
const isObject = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);

/** An own enumerable property set whatever its key: a "__proto__" key stays a key, never the prototype. */
function setOwn<T>(o: Record<string, T>, key: string, value: T): void {
  Object.defineProperty(o, key, { value, enumerable: true, writable: true, configurable: true });
}

/** The entry's single import map, parsed, or why there is none. A null value is kept: it blocks its specifier. */
export function importMapOf(facts: HtmlFacts): ImportMapRead {
  if (facts.importMaps.length === 0) return { map: null, error: 'index.html holds no <script type="importmap">' };
  if (facts.importMaps.length > 1) return { map: null, error: `index.html holds ${facts.importMaps.length} import maps: a page holds one` };
  const only = facts.importMaps[0]!;
  if (only.error !== null) return { map: null, error: `index.html's import map is not JSON (${only.error})` };
  if (!isObject(only.value)) return { map: null, error: 'index.html\'s import map is not a JSON object' };
  const importsRaw = only.value.imports;
  if (!isObject(importsRaw)) return { map: null, error: 'index.html\'s import map holds no "imports" object' };
  const imports: Record<string, string | null> = {};
  for (const [key, value] of Object.entries(importsRaw)) {
    if (value !== null && typeof value !== 'string') return { map: null, error: `index.html's import map maps ${JSON.stringify(key)} to no string` };
    setOwn(imports, key, value);
  }
  const scopes: Record<string, Record<string, string | null>> = {};
  const scopesRaw = only.value.scopes;
  if (scopesRaw !== undefined) {
    if (!isObject(scopesRaw)) return { map: null, error: 'index.html\'s import map holds a "scopes" that is not an object' };
    for (const [scope, entries] of Object.entries(scopesRaw)) {
      if (!isObject(entries)) return { map: null, error: `index.html's import map scope ${JSON.stringify(scope)} is not an object` };
      const inner: Record<string, string | null> = {};
      for (const [key, value] of Object.entries(entries)) {
        if (value !== null && typeof value !== 'string') return { map: null, error: `index.html's import map scope ${JSON.stringify(scope)} maps ${JSON.stringify(key)} to no string` };
        setOwn(inner, key, value);
      }
      setOwn(scopes, scope, inner);
    }
  }
  return { map: { imports, scopes }, error: null };
}

/** A URI scheme's leading `name:`. */
const URI_SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;

/** Whether a reference is relative: not empty, no scheme, no leading // and no leading /. */
export function isRelativeRef(ref: string): boolean {
  return ref.length > 0 && !URI_SCHEME.test(ref) && !ref.startsWith('//') && !ref.startsWith('/');
}

/**
 * Whether a module specifier is relative in JavaScript's sense: it starts with ./ or ../ (or is . or ..). A
 * specifier like `three` or `main.js` is bare: a browser resolves it through the import map or fails, it never
 * loads a file beside the importer. isRelativeRef answers for a URL reference (an attribute or an import-map value),
 * where `main.js` is relative to the page.
 */
export function isRelativeSpecifier(specifier: string): boolean {
  return specifier === '.' || specifier === '..' || specifier.startsWith('./') || specifier.startsWith('../');
}

/**
 * The path under game/ that the relative reference `ref` names, resolved against the importing file's path
 * (`from`, POSIX and relative to game/, like `src/main.js`; index.html is `index.html`). A query or fragment is
 * dropped. Null when `ref` is not relative, names nothing, or climbs out of game/.
 */
export function resolveGamePath(ref: string, from: string): string | null {
  if (!isRelativeRef(ref)) return null;
  const cut = ref.search(/[?#]/);
  const raw = cut < 0 ? ref : ref.slice(0, cut);
  if (raw === '') return null;
  const parts = from.split('/').slice(0, -1);
  for (const seg of raw.split('/')) {
    if (seg === '' || seg === '.') continue;
    if (seg === '..') {
      if (parts.length === 0) return null;
      parts.pop();
      continue;
    }
    parts.push(seg);
  }
  if (parts.length === 0) return null;
  return parts.join('/');
}

/** What an import map says about a specifier: found false when it maps nothing, or value null when a null entry blocks it. */
export interface ImportMapMatch {
  found: boolean;
  value: string | null;
}

/** The entry for a specifier in one map: an exact own key, else the longest own key ending in / that prefixes it. */
function entryOf(entries: Record<string, string | null>, specifier: string): ImportMapMatch {
  if (Object.hasOwn(entries, specifier)) return { found: true, value: entries[specifier]! };
  let best: string | null = null;
  for (const key of Object.keys(entries)) {
    if (key.endsWith('/') && specifier.startsWith(key) && (best === null || key.length > best.length)) best = key;
  }
  if (best === null) return { found: false, value: null };
  const base = entries[best]!;
  if (base === null) return { found: true, value: null };
  return { found: true, value: `${base}${specifier.slice(best.length)}` };
}

/**
 * The game path a scope key's URL prefix names, or null when it cannot name one: a reference resolved against
 * index.html, with '.' and './' being game/ itself. An absolute scope names a site path, not a file of the game.
 */
function scopePath(scope: string): string | null {
  const cut = scope.search(/[?#]/);
  const raw = cut < 0 ? scope : scope.slice(0, cut);
  if (raw === '.' || raw === './') return '';
  return resolveGamePath(scope, INDEX_HTML);
}

/**
 * What the page's import map says for `specifier` imported from `from` (a path under game/, index.html for an inline
 * script): a matching scope's map first, its longest URL prefix winning, then the top-level imports.
 */
export function importMapMatch(map: ImportMap | null, specifier: string, from: string = INDEX_HTML): ImportMapMatch {
  if (map === null) return { found: false, value: null };
  let best: { prefix: string; entries: Record<string, string | null> } | null = null;
  for (const [scope, entries] of Object.entries(map.scopes)) {
    const prefix = scopePath(scope);
    if (prefix === null || !from.startsWith(prefix)) continue;
    if (best === null || prefix.length > best.prefix.length) best = { prefix, entries };
  }
  if (best !== null) {
    const scoped = entryOf(best.entries, specifier);
    if (scoped.found) return scoped;
  }
  return entryOf(map.imports, specifier);
}

/**
 * The value an import map gives a bare specifier imported from `from`: a matching scope's entry or the top-level
 * one, an exact key first then the longest key ending in / that prefixes it; null when none maps it or a null entry
 * blocks it.
 */
export function mappedSpecifier(map: ImportMap | null, specifier: string, from: string = INDEX_HTML): string | null {
  const match = importMapMatch(map, specifier, from);
  return match.found ? match.value : null;
}

/** The path a specifier names from `from`: relative to the importer, or through the import map; null when neither gives a path under game/. */
function resolveSpecifier(specifier: string, from: string, map: ImportMap | null): string | null {
  if (isRelativeSpecifier(specifier)) return resolveGamePath(specifier, from);
  const match = importMapMatch(map, specifier, from);
  if (!match.found || match.value === null || !isRelativeRef(match.value)) return null;
  return resolveGamePath(match.value, INDEX_HTML);
}

// ---------------------------------------------------------------- static imports

/** What a lexical scan of a module's text is doing, character by character. */
const IDENT_START = /[A-Za-z_$]/;
const IDENT = /[A-Za-z0-9_$]/;

/**
 * A module's static import and export-from specifiers, in source order, by a lexical scan: comments, strings,
 * template literals and regular-expression literals are skipped, so the words inside them are never read as
 * imports. This is not a parser; it recognizes `import 'x'`, `import ... from 'x'`, `export ... from 'x'` and
 * `export * from 'x'`, and leaves anything else alone.
 */
export function sourceImports(source: string): string[] {
  const out: string[] = [];
  const n = source.length;
  let i = 0;
  /** The last significant (non-space, non-comment) character, for the regex-versus-division guess. */
  const regexPosition = (prev: string): boolean => prev === '' || '([{,;:=!&|?+-*%^~<>'.includes(prev);
  let prev = '';

  const skipSpace = (): void => {
    for (;;) {
      while (i < n && /\s/.test(source[i]!)) i++;
      if (source.startsWith('//', i)) {
        while (i < n && source[i] !== '\n') i++;
        continue;
      }
      if (source.startsWith('/*', i)) {
        const end = source.indexOf('*/', i + 2);
        i = end < 0 ? n : end + 2;
        continue;
      }
      return;
    }
  };
  const skipString = (quote: string): string | null => {
    i++;
    let s = '';
    while (i < n) {
      const ch = source[i]!;
      if (ch === quote) {
        i++;
        return s;
      }
      if (ch === '\\') {
        const next = source[i + 1];
        if (next === undefined) return null;
        s += next === 'n' ? '\n' : next === 't' ? '\t' : next === 'r' ? '\r' : next;
        i += 2;
        continue;
      }
      if (ch === '\n') return null;
      s += ch;
      i++;
    }
    return null;
  };
  const skipTemplate = (): void => {
    i++;
    while (i < n) {
      const ch = source[i]!;
      if (ch === '\\') {
        i += 2;
        continue;
      }
      if (ch === '`') {
        i++;
        return;
      }
      i++;
    }
  };
  const skipRegex = (): void => {
    i++;
    let inClass = false;
    while (i < n) {
      const ch = source[i]!;
      if (ch === '\\') {
        i += 2;
        continue;
      }
      if (ch === '[') inClass = true;
      else if (ch === ']') inClass = false;
      else if (ch === '/' && !inClass) {
        i++;
        return;
      } else if (ch === '\n') return;
      i++;
    }
  };
  const readWord = (): string => {
    const start = i;
    while (i < n && IDENT.test(source[i]!)) i++;
    return source.slice(start, i);
  };
  /** Reads a string literal at i, or null; advances past it either way. */
  const maybeString = (): string | null => {
    skipSpace();
    const ch = source[i];
    if (ch !== '"' && ch !== "'") return null;
    return skipString(ch);
  };
  /** From just after `import`/`export`, reads a clause and its `from 'x'`; returns the specifier or null. */
  const clause = (stopAt: string[]): string | null => {
    for (;;) {
      skipSpace();
      if (i >= n) return null;
      const ch = source[i]!;
      if (stopAt.includes(ch)) return null;
      if (ch === '"' || ch === "'") return skipString(ch);
      if (ch === '{' || ch === '}') {
        i++;
        continue;
      }
      if (ch === ',' || ch === '*') {
        i++;
        continue;
      }
      if (IDENT_START.test(ch)) {
        const word = readWord();
        if (word === 'from') {
          const spec = maybeString();
          return spec;
        }
        if (word === 'as' || word === 'type') continue;
        continue;
      }
      return null;
    }
  };

  while (i < n) {
    skipSpace();
    if (i >= n) break;
    const ch = source[i]!;
    if (ch === '"' || ch === "'") {
      skipString(ch);
      prev = ch;
      continue;
    }
    if (ch === '`') {
      skipTemplate();
      prev = '`';
      continue;
    }
    if (ch === '/') {
      if (regexPosition(prev)) skipRegex();
      else i++;
      prev = '/';
      continue;
    }
    if (IDENT_START.test(ch)) {
      const word = readWord();
      prev = 'a';
      if (word === 'import') {
        skipSpace();
        if (source[i] === '(' || source[i] === '.') continue;
        const spec = clause(['=', ';']);
        if (spec !== null) out.push(spec);
        continue;
      }
      if (word === 'export') {
        skipSpace();
        if (source[i] !== '*' && source[i] !== '{') continue;
        const spec = clause(['=', ';']);
        if (spec !== null) out.push(spec);
        continue;
      }
      continue;
    }
    prev = ch;
    i++;
  }
  return out;
}

/** A source file of a game: its path under game/ and its bytes. */
export interface GameSource {
  path: string;
  bytes: Uint8Array;
}

/**
 * The source JS modules of a game in the order a depth-first walk of static imports reaches them from
 * index.html's module scripts: each <script type="module"> in document order, its src resolved relative to
 * index.html or its inline text's imports so resolved, then each reached module's own imports in source order,
 * the first time a path is reached. Specifiers are resolved relative to the importer or through the page's import
 * map (the importing module's matching scope's entry or the top-level one; a value resolves against index.html); a
 * specifier that resolves to no source JS module is skipped, and a module reached once is never walked twice.
 * `sources` is every source file of game/ (index.html included, which is never in the result).
 */
export function importOrder(sources: readonly GameSource[], facts: HtmlFacts): string[] {
  const byPath = new Map(sources.map((s) => [s.path, s]));
  const map = importMapOf(facts).map;
  const textOf = (path: string): string => {
    const file = byPath.get(path);
    return file === undefined ? '' : text(file.bytes);
  };
  const order: string[] = [];
  const seen = new Set<string>();
  const visit = (path: string): void => {
    if (seen.has(path) || !MODULE_NAME.test(path) || !byPath.has(path)) return;
    seen.add(path);
    order.push(path);
    for (const specifier of sourceImports(textOf(path))) {
      const target = resolveSpecifier(specifier, path, map);
      if (target !== null) visit(target);
    }
  };
  for (const script of facts.scripts) {
    if (script.type !== 'module') continue;
    if (script.src !== null) {
      // A script's src is a URL reference, not a module specifier: `main.js` is relative to the page.
      const target = isRelativeRef(script.src) ? resolveGamePath(script.src, INDEX_HTML) : resolveSpecifier(script.src, INDEX_HTML, map);
      if (target !== null) visit(target);
    } else {
      for (const specifier of sourceImports(script.text)) {
        const target = resolveSpecifier(specifier, INDEX_HTML, map);
        if (target !== null) visit(target);
      }
    }
  }
  return order;
}

// ---------------------------------------------------------------- canonical JSON

/**
 * JSON with every object's keys in code-unit order and no whitespace: one spelling per value. Takes
 * null, booleans, finite numbers, strings, arrays and plain objects; throws TypeError on anything else
 * (undefined, a function, a bigint, NaN, Infinity, a class instance).
 */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError(`canonicalJson: ${value} is not a finite number`);
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map((v) => canonicalJson(v)).join(',')}]`;
  if (typeof value === 'object') {
    const proto = Object.getPrototypeOf(value);
    if (proto !== Object.prototype && proto !== null) throw new TypeError('canonicalJson: only plain objects and arrays');
    const o = value as Record<string, unknown>;
    return `{${Object.keys(o)
      .sort(byCodeUnit)
      .map((k) => `${JSON.stringify(k)}:${canonicalJson(o[k])}`)
      .join(',')}}`;
  }
  throw new TypeError(`canonicalJson: a ${typeof value} has no JSON form`);
}

// ---------------------------------------------------------------- the dossier

const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const JPEG_MAGIC = [0xff, 0xd8, 0xff];
/** contracts/dossier.ts DossierImageName. */
const IMAGE_NAME = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$/;

const opensWith = (bytes: Uint8Array, magic: readonly number[], at = 0): boolean => bytes.length >= at + magic.length && magic.every((b, i) => bytes[at + i] === b);

/** A dossier image's extension's group: a file's bytes grouped by the extension the Game block counts. */
const extensionOf = (path: string): string => {
  const base = path.split('/').pop() ?? '';
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : '(none)';
};

/** The `## Game` block's lines: what game/ holds, by class, with the totals (no source text). */
function gameBlockLines(tree: GameTree, threePath: string | null, threeBytes: number, importValue: string | null): string[] {
  const source = tree.files.filter((f) => f.cls === 'source');
  const vendored = tree.files.filter((f) => f.cls === 'vendored');
  const assets = tree.files.filter((f) => f.cls === 'asset');
  const sum = (list: readonly GameFile[]): number => list.reduce((n, f) => n + f.bytes, 0);
  const lines = [
    '## Game',
    '',
    threePath === null ? `three.js ${THREE_VERSION}: not found (no import map "three" naming a file of game/)` : `three.js ${THREE_VERSION}: game/${threePath} (${threeBytes} bytes)`,
    `Import map "three" → ${importValue ?? '(no import map)'}`,
    `Source files: ${source.length} (${sum(source)} bytes)`,
    `Vendored files: ${vendored.length} (${sum(vendored)} bytes)`,
  ];
  if (vendored.length > 0) {
    const listed = vendored.slice(0, VENDORED_LISTED).map((f) => `game/${f.path} (${f.bytes} bytes)`);
    const more = vendored.length > VENDORED_LISTED ? `, and ${vendored.length - VENDORED_LISTED} more` : '';
    lines.push(`  ${listed.join(', ')}${more}`);
  }
  const byExt = new Map<string, { n: number; bytes: number }>();
  for (const f of assets) {
    const ext = extensionOf(f.path);
    const at = byExt.get(ext) ?? { n: 0, bytes: 0 };
    at.n++;
    at.bytes += f.bytes;
    byExt.set(ext, at);
  }
  const extText = [...byExt]
    .sort((a, b) => byCodeUnit(a[0], b[0]))
    .map(([ext, at]) => `${ext} ${at.n} (${at.bytes} bytes)`)
    .join(', ');
  lines.push(`Assets: ${extText === '' ? 'none' : extText}`);
  lines.push(`Totals: ${tree.count} files and directories (${tree.totalBytes} bytes)`);
  return lines;
}

/** A distinct source as the dossier shows it. */
interface SourceCode {
  norm: Uint8Array;
  text: string;
  fence: string;
  /** The fenced block: the fence and its language, the text (a last LF added when it lacks one), the fence, a blank line. */
  block: string;
  blockBytes: number;
  lines: number;
}

/**
 * A source's section cut inside its text: its heading, its first k lines in its fence (closed), a line naming how
 * many of its lines are not shown, then `tail(hidden)` (the cut line, given the source bytes this file leaves out),
 * k the most lines (at least one, fewer than all) whose whole fits in `room` UTF-8 bytes; null when not one line
 * fits. It cuts only after a line's LF, which is never inside a UTF-8 sequence nor a replacement character's bytes,
 * so no character is split, and a line's shown bytes are those of its text.
 */
function sourceHead(heading: string, code: { norm: Uint8Array; text: string; fence: string }, language: string, room: number, tail: (hidden: number) => string): string | null {
  const total = lineCount(code.norm);
  const lines = code.text.split('\n');
  const open = `${code.fence}${language}\n`;
  const close = `${code.fence}\n\n`;
  const note = (k: number): string => `[${total - k} of this file's ${total} lines not shown.]\n`;
  const fixed = byteLength(heading) + byteLength(open) + byteLength(close);
  let best = 0;
  let shown = 0;
  let raw = 0;
  for (let k = 1; k < total; k++) {
    shown += byteLength(lines[k - 1]!) + 1;
    raw = code.norm.indexOf(0x0a, raw) + 1;
    if (fixed + shown + byteLength(note(k)) + byteLength(tail(code.norm.length - raw)) <= room) best = k;
  }
  if (best === 0) return null;
  let rawEnd = 0;
  for (let k = 0; k < best; k++) rawEnd = code.norm.indexOf(0x0a, rawEnd) + 1;
  const head = lines.slice(0, best).map((l) => `${l}\n`).join('');
  return `${heading}${open}${head}${close}${note(best)}${tail(code.norm.length - rawEnd)}`;
}

/**
 * The dossier (makefun/dossier/v1, contracts/dossier.ts) of an entry directory, as canonical JSON bytes:
 * `dir/game/`'s sources and `dir/screenshots/`'s PNG and JPEG files (names starting with '.' skipped). `images` are
 * the screenshots in name order, the first the cover; `text` is a "## Game" block (the three.js module found, the
 * import map's "three" value, the source, vendored and asset counts and the totals) then "## Sources", index.html
 * first, then the source JS modules in importOrder's order, then the other source files by path, each under a
 * header line naming its path, lines and bytes and then its text in a fence. The text holds at most
 * `caps.text_max_bytes` UTF-8 bytes: every source that fits goes in whole, in that order; the first that does not
 * goes in up to its last complete line that fits (its fence closed, then a line naming how many of its lines are not
 * shown), or not at all when not one line fits; nothing follows it but a last line naming how many whole files and
 * how many bytes of source were left out. Throws DossierError on a game tree it cannot read or one past a cap
 * (readGameTree), a missing game/index.html, screenshots no dossier can hold, a text cap that cannot hold the Game
 * block and the cut line, or a dossier past DOSSIER_MAX_BYTES.
 */
export function buildDossier(dir: string, caps: DossierCaps): Uint8Array {
  const max = caps.text_max_bytes;
  if (!Number.isSafeInteger(max) || max <= 0) throw new DossierError(`buildDossier: caps.text_max_bytes must be a positive integer, not ${String(max)}`, 'text_max_bytes');
  // The binding screenshot caps, each with whose it is: the caller's when given and tighter, else the dossier's.
  let maxImages = { n: DOSSIER_MAX_IMAGES, whose: 'a dossier holds' };
  let maxImageBytes = { n: DOSSIER_MAX_BYTES, whose: 'a dossier holds' };
  if (caps.screenshots !== undefined) {
    const s = caps.screenshots as { max?: unknown; max_bytes?: unknown } | null;
    if (typeof s !== 'object' || s === null) throw new DossierError('buildDossier: caps.screenshots must be an object {max, max_bytes}');
    for (const k of ['max', 'max_bytes'] as const) {
      const v = s[k];
      if (typeof v !== 'number' || !Number.isSafeInteger(v) || v <= 0) throw new DossierError(`buildDossier: caps.screenshots.${k} must be a positive integer, not ${String(v)}`, `screenshots.${k}`);
    }
    if ((s.max as number) < maxImages.n) maxImages = { n: s.max as number, whose: 'caps.screenshots.max allows' };
    if ((s.max_bytes as number) < maxImageBytes.n) maxImageBytes = { n: s.max_bytes as number, whose: 'caps.screenshots.max_bytes allows' };
  }
  const gameCaps: GameCaps = {};
  for (const k of ['game_max_files', 'game_max_bytes', 'game_path_max_bytes'] as const) {
    const v = caps[k] as unknown;
    if (v === undefined) continue;
    if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < 0) throw new DossierError(`buildDossier: caps.${k} must be a whole number, not ${String(v)}`, k);
    gameCaps[k] = v;
  }

  const gameDir = join(dir, 'game');
  const tree = readGameTree(gameDir, gameCaps);
  const htmlBytes = readIndexHtml(gameDir, tree);
  const html = htmlFacts(text(htmlBytes));
  const mapped = importMapOf(html);
  const importValue = mapped.map !== null && Object.hasOwn(mapped.map.imports, 'three') ? mapped.map.imports['three']! : null;

  // The three.js module the import map names, when it names one inside game/; else one the tree holds by name.
  let threePath: string | null = null;
  let threeBytes = 0;
  if (importValue !== null && isRelativeRef(importValue)) {
    const target = resolveGamePath(importValue, INDEX_HTML);
    if (target !== null) {
      const file = tree.files.find((f) => f.path === target);
      if (file !== undefined) {
        threePath = file.path;
        threeBytes = file.bytes;
      }
    }
  }
  if (threePath === null) {
    const named = tree.files.filter((f) => f.path === THREE_MODULE_FILE || f.path.endsWith(`/${THREE_MODULE_FILE}`));
    if (named.length === 1) {
      threePath = named[0]!.path;
      threeBytes = named[0]!.bytes;
    }
  }

  const sources = tree.files.filter((f) => f.cls === 'source').map((f) => ({ path: f.path, bytes: new Uint8Array(readFileSync(join(gameDir, f.path))) }));
  const byPath = new Map(sources.map((s) => [s.path, s]));
  const ordered = importOrder(sources, html).filter((p) => p !== INDEX_HTML);
  const seen = new Set<string>([INDEX_HTML, ...ordered]);
  const rest = sources.map((s) => s.path).filter((p) => !seen.has(p)).sort(byCodeUnit);
  const order = [INDEX_HTML, ...ordered, ...rest];

  const codes = new Map<string, SourceCode>();
  const codeOf = (path: string): SourceCode | null => {
    let code = codes.get(path);
    if (code !== undefined) return code;
    const file = byPath.get(path);
    if (file === undefined) return null;
    const t = text(file.bytes);
    const fence = fenceFor(t);
    const block = `${fence}${languageOf(path)}\n${t}${t.endsWith('\n') ? '' : '\n'}${fence}\n\n`;
    code = { norm: file.bytes, text: t, fence, block, blockBytes: byteLength(block), lines: lineCount(file.bytes) };
    codes.set(path, code);
    return code;
  };

  const shotNames = (() => {
    const shotsDir = join(dir, 'screenshots');
    let shotDirStat;
    try {
      shotDirStat = lstatSync(shotsDir);
    } catch {
      throw new DossierError('buildDossier: no screenshots/ directory: an entry hands in 3 or 4 renders of its game, the first by name the cover');
    }
    if (!shotDirStat.isDirectory()) throw new DossierError('buildDossier: screenshots is not a directory: an entry hands in 3 or 4 renders of its game, the first by name the cover');
    let names: string[];
    try {
      names = readdirSync(shotsDir, { withFileTypes: true })
        .filter((e) => e.isFile() && !e.name.startsWith('.'))
        .map((e) => e.name)
        .sort(byCodeUnit);
    } catch (err) {
      throw new DossierError(`buildDossier: screenshots/ cannot be read (${(err as Error).message})`);
    }
    return names;
  })();
  if (shotNames.length === 0) throw new DossierError('buildDossier: screenshots/ holds no image: the first is the cover');
  if (shotNames.length > maxImages.n) throw new DossierError(`buildDossier: screenshots/ holds ${shotNames.length} files, over the ${maxImages.n} ${maxImages.whose}`, 'screenshots.max');
  const images = shotNames.map((name) => {
    if (!IMAGE_NAME.test(name)) throw new DossierError(`buildDossier: screenshots/${name}: a name is letters, digits and . _ -, 1 to 128 characters, not starting with . or -`);
    const size = lstatSync(join(dir, 'screenshots', name)).size;
    if (size > maxImageBytes.n) throw new DossierError(`buildDossier: screenshots/${name} is ${size} bytes, over the ${maxImageBytes.n} ${maxImageBytes.whose}`, 'screenshots.max_bytes');
    const bytes = new Uint8Array(readFileSync(join(dir, 'screenshots', name)));
    if (bytes.length > maxImageBytes.n) throw new DossierError(`buildDossier: screenshots/${name} is ${bytes.length} bytes, over the ${maxImageBytes.n} ${maxImageBytes.whose}`, 'screenshots.max_bytes');
    const media_type = opensWith(bytes, PNG_MAGIC) ? 'image/png' : opensWith(bytes, JPEG_MAGIC) ? 'image/jpeg' : null;
    if (media_type === null) throw new DossierError(`buildDossier: screenshots/${name} is neither a PNG nor a JPEG`);
    return { name, media_type, sha256: sha256Hex(bytes), base64: Buffer.from(bytes).toString('base64') };
  });

  const head = [...gameBlockLines(tree, threePath, threeBytes, importValue), '', '## Sources', '', ''].join('\n');
  const sections = order.map((path) => {
    const code = codeOf(path);
    const lines = code === null ? 0 : code.lines;
    const bytes = code === null ? 0 : code.norm.length;
    const heading = `### game/${path} (${lines} ${lines === 1 ? 'line' : 'lines'}, ${bytes} bytes)\n\n`;
    const shown = lines === 0 ? null : code;
    return { heading, code: shown, size: byteLength(heading) + (shown === null ? 0 : shown.blockBytes), bytes };
  });
  // restBytes[k]: the source bytes of the files after section k.
  const restBytes = new Array<number>(sections.length + 1).fill(0);
  for (let k = sections.length - 1; k >= 0; k--) restBytes[k] = restBytes[k + 1]! + (k + 1 < sections.length ? sections[k + 1]!.bytes : 0);

  const cutLine = (files: number, bytes: number): string => `[Cut at ${max} bytes: ${files} file${files === 1 ? '' : 's'} and ${bytes} bytes of source left out.]\n`;
  let out = head;
  let used = byteLength(head);
  let kept = 0;
  for (; kept < sections.length; kept++) {
    const section = sections[kept]!;
    const restCount = sections.length - kept - 1;
    const tail = restCount === 0 ? 0 : byteLength(cutLine(restCount, restBytes[kept]!));
    if (used + section.size + tail > max) break;
    out += section.code === null ? section.heading : `${section.heading}${section.code.block}`;
    used += section.size;
  }
  if (kept < sections.length) {
    // The first source that does not fit whole: its head, up to its last complete line that fits, then nothing more.
    const first = sections[kept]!;
    const restCount = sections.length - kept - 1;
    const after = restBytes[kept]!;
    const part = first.code === null ? null : sourceHead(first.heading, first.code, languageOf(order[kept]!), max - used, (hidden) => cutLine(restCount, after + hidden));
    out += part ?? cutLine(restCount + 1, after + first.bytes);
  }
  if (byteLength(out) > max) throw new DossierError(`buildDossier: caps.text_max_bytes ${max} cannot hold even the Game block and the cut line`, 'text_max_bytes');

  const doc = { schema: DOSSIER_SCHEMA, cover: 0, text: out.replace(/\n+$/, '\n'), images };
  const bytes = Buffer.from(canonicalJson(doc), 'utf8');
  if (bytes.length > DOSSIER_MAX_BYTES) throw new DossierError(`buildDossier: the dossier is ${bytes.length} bytes, over the ${DOSSIER_MAX_BYTES} one holds`);
  return new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.length);
}
