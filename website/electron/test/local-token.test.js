"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const nodeFs = require("node:fs");
const nodeHttp = require("node:http");
const nodeOs = require("node:os");
const path = require("node:path");
const { describe, it } = require("node:test");
const {
  mintLocalToken,
  listenerSecretPath,
  ownedSocketPath,
  dashboardSocketName,
  dialTarget,
} = require("../local-token");

// Keys are built with path.join, matching how mintLocalToken composes the
// credential path: a POSIX literal would never match the backslash-separated
// path the real path.join produces on Windows, so the fake fs would return
// undefined and these tests would fail on path syntax rather than on the rules
// they exist to pin.
const CANONICAL_HOME = path.resolve(path.sep, "canonical");
const LEGACY_HOME = path.resolve(path.sep, "legacy");

/** The port-keyed name the gateway also publishes for port-only readers. */
function portKeyedPath(home, port) {
  return path.join(home, "run", `gateway-${port}.secret`);
}

/** An fs whose readFileSync answers from `files` and throws for anything else. */
function fakeFsOver(files, reads = []) {
  return {
    readFileSync(p) {
      reads.push(p);
      if (!files.has(p)) {
        const error = new Error(`ENOENT: ${p}`);
        error.code = "ENOENT";
        throw error;
      }
      return files.get(p);
    },
  };
}

/** An http whose get() records the secret sent and answers with `statusCode`. */
function fakeHttpRecording({ statusCode = 200, token = "minted-token", sent = [], urls = [] }) {
  return {
    get(url, options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      sent.push(options.headers["X-Local-Secret"]);
      urls.push(url);
      const response = new EventEmitter();
      response.statusCode = statusCode;
      response.resume = () => {};
      queueMicrotask(() => {
        callback(response);
        if (statusCode === 200) {
          response.emit("data", JSON.stringify({ token }));
          response.emit("end");
        }
      });
      return request;
    },
  };
}

describe("mintLocalToken", () => {
  it("sends the credential the dialed listener published, to literal IPv4 loopback", async () => {
    const files = new Map([
      [listenerSecretPath(LEGACY_HOME, "5476", "127.0.0.1", path), "v4-loopback-secret"],
    ]);
    const sent = [];
    const urls = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => LEGACY_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent, urls }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["v4-loopback-secret"]);
    assert.deepEqual(urls, ["http://127.0.0.1:5476/api/token/local"]);
  });

  it("accepts a wildcard v4 bind, which answers the dialed loopback address", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "wildcard-secret"],
    ]);
    const sent = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["wildcard-secret"]);
  });

  it("refuses when only another bind address's listener published a credential", async () => {
    // THE PIN. A gateway bound to the v6 loopback leaves IPv4 127.0.0.1:5476
    // free, so an `ssh -L 127.0.0.1:5476` tunnel can hold the address this call
    // dials. Its entry, and the port-keyed entry that names every listener on
    // 5476, both hold the v6 gateway's credential. Neither may be read: the
    // credential would go down the tunnel to a remote peer, unrecoverably.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "__1", path), "v6-gateway-secret"],
      [portKeyedPath(CANONICAL_HOME, "5476"), "v6-gateway-secret"],
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    const reads = [];
    let called = false;

    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files, reads),
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false, "no secret reaches the dialed address");
    assert.deepEqual(reads, [
      listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path),
      listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path),
    ], "only entries naming a listener on the dialed address are even read");
  });

  it("refuses the home-wide secret when no listener on this address published one", async () => {
    const files = new Map([
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    let called = false;

    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false, "no secret reaches the port");
  });

  it("prefers the exact dialed address over the wildcard entry", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "exact-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "wildcard-secret"],
    ]);
    const sent = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["exact-secret"]);
  });

  it("keys the credential to the dialed port, not to a sibling listener", async () => {
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "secret-5476"],
      [listenerSecretPath(CANONICAL_HOME, "9099", "127.0.0.1", path), "secret-9099"],
    ]);
    const sent = [];
    const urls = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:9099",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ sent, urls }),
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["secret-9099"]);
    assert.deepEqual(urls, ["http://127.0.0.1:9099/api/token/local"]);
  });

  it("tries the other covering address when a stale entry is refused", async () => {
    // `clear_marker` runs only on a graceful shutdown, so a crashed gateway
    // leaves its entry behind. A stale exact-address entry beside the live
    // wildcard one must not spend the walk: a dead generation's secret
    // authenticates against no listener, and both candidates name a listener on
    // the address being dialed.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "stale-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "live-secret"],
    ]);
    const sent = [];
    const fakeHttp = {
      get(url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const secret = options.headers["X-Local-Secret"];
        sent.push(secret);
        const response = new EventEmitter();
        response.statusCode = secret === "live-secret" ? 200 : 403;
        response.resume = () => {};
        queueMicrotask(() => {
          callback(response);
          if (response.statusCode === 200) {
            response.emit("data", JSON.stringify({ token: "minted-token" }));
            response.emit("end");
          }
        });
        return request;
      },
    };

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttp,
    });

    assert.equal(token, "minted-token");
    assert.deepEqual(sent, ["stale-secret", "live-secret"]);
  });

  it("never reaches the port-keyed or home-wide secret, even when every entry is refused", async () => {
    // The boundary is WHICH entries may be tried, not how many attempts are
    // made. Entries naming a listener on the dialed address are fair game; an
    // entry that merely shares the port number, and the home-wide file, are not.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "exact-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "0.0.0.0", path), "wildcard-secret"],
      [portKeyedPath(CANONICAL_HOME, "5476"), "port-keyed-secret"],
      [path.join(CANONICAL_HOME, ".local_secret"), "home-wide-secret"],
    ]);
    const sent = [];

    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ statusCode: 403, sent }),
    });

    assert.equal(token, "");
    assert.deepEqual(sent, ["exact-secret", "wildcard-secret"]);
    assert.equal(sent.includes("port-keyed-secret"), false);
    assert.equal(sent.includes("home-wide-secret"), false);
  });

  it("refuses to send a local secret to a non-literal remote address", async () => {
    let called = false;
    const reads = [];
    const token = await mintLocalToken({
      backendUrl: "http://example.com:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(new Map(), reads),
      http: { get: () => { called = true; } },
    });

    assert.equal(token, "");
    assert.equal(called, false);
    assert.deepEqual(reads, [], "a non-loopback target is refused before any credential is read");
  });

  it("dials the host exactly as written, never a rewritten spelling", async () => {
    // THE PIN for B. The document stays on the configured origin, so the mint
    // must not move the request either: `localhost` is dialed as `localhost`.
    // Rewriting it to a literal is what would split the document's storage and
    // break every comparison that holds the configured string.
    const files = new Map([
      [listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path), "shared-secret"],
      [listenerSecretPath(CANONICAL_HOME, "5476", "::1", path), "shared-secret"],
    ]);
    const urls = [];

    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => CANONICAL_HOME,
      path,
      fs: fakeFsOver(files),
      http: fakeHttpRecording({ urls }),
    });

    assert.equal(token, "minted-token");
    assert.equal(new URL(urls[0]).origin, "http://localhost:5476", "dialed as written");
    assert.equal(
      urls.filter((u) => u.includes("127.0.0.1")).length,
      0,
      "no request is addressed to a rewritten literal",
    );
  });

  it("refuses a name when one family it reaches has no entry", async () => {
    // THE SECURITY PIN. `localhost` reaches both loopback families, so a
    // credential sent there can land on whichever family this gateway does NOT
    // hold -- free for a co-resident. Holding only one family is therefore not
    // enough to earn the name, in EITHER direction, and the cost of refusing is
    // one explicit sign-in.
    for (const held of ["127.0.0.1", "::1"]) {
      const files = new Map([
        [listenerSecretPath(CANONICAL_HOME, "5476", held, path), "one-family-secret"],
      ]);
      let called = false;

      const token = await mintLocalToken({
        backendUrl: "http://localhost:5476",
        resolveHome: () => CANONICAL_HOME,
        path,
        fs: fakeFsOver(files),
        http: {
          get: () => {
            called = true;
          },
        },
      });

      assert.equal(token, "", held);
      assert.equal(called, false, `no request is made while holding only ${held}`);
    }
  });

  it("refuses a non-http scheme and an unparseable origin", async () => {
    for (const backendUrl of ["https://localhost:5476", "not a url"]) {
      let called = false;
      const reads = [];
      const token = await mintLocalToken({
        backendUrl,
        resolveHome: () => CANONICAL_HOME,
        path,
        fs: fakeFsOver(new Map(), reads),
        http: { get: () => { called = true; } },
      });

      assert.equal(token, "", backendUrl);
      assert.equal(called, false, backendUrl);
      assert.deepEqual(reads, [], backendUrl);
    }
  });
});

describe("listenerSecretPath", () => {
  it("names the file the gateway publishes for one listener", () => {
    assert.equal(
      listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path),
      path.join(CANONICAL_HOME, "run", "gateway-5476-127.0.0.1.secret"),
    );
  });

  it("is a different file per bind address on one port", () => {
    assert.notEqual(
      listenerSecretPath(CANONICAL_HOME, "5476", "127.0.0.1", path),
      listenerSecretPath(CANONICAL_HOME, "5476", "__1", path),
    );
  });
});

const OWN_UID = 501;
const OTHER_UID = 777;
const SOCKET_HOME = path.resolve(path.sep, "users", "me", ".kiro", "crew");

/** A stat result for a directory or a socket. */
function dirStat(uid, mode) {
  return { isDirectory: () => true, isSocket: () => false, uid, mode };
}

/**
 * An fs over a POSIX-shaped tree: `home` and its socket are this user's, every
 * other ancestor is root's and 0755 unless `dirs` says otherwise.
 */
function fakeTree({
  home = SOCKET_HOME,
  homeStat = dirStat(OWN_UID, 0o40700),
  dirs = new Map(),
  socket = { uid: OWN_UID, isSocket: true },
  port = "5476",
  files = new Map(),
  realpath = (p) => p,
  reads = [],
} = {}) {
  const socketPath = path.join(realpath(home), dashboardSocketName(port));
  return {
    realpathSync: (p) => realpath(p),
    statSync(p) {
      if (p === realpath(home)) return homeStat;
      if (dirs.has(p)) return dirs.get(p);
      return dirStat(0, 0o40755);
    },
    lstatSync(p) {
      if (p === socketPath && socket) {
        return { isDirectory: () => false, isSocket: () => socket.isSocket, uid: socket.uid, mode: 0o140600 };
      }
      const error = new Error(`ENOENT: ${p}`);
      error.code = "ENOENT";
      throw error;
    },
    readFileSync(p) {
      reads.push(p);
      if (!files.has(p)) {
        const error = new Error(`ENOENT: ${p}`);
        error.code = "ENOENT";
        throw error;
      }
      return files.get(p);
    },
  };
}

/**
 * An http that records every send with its transport. `onSocket`/`onTcp` decide
 * the outcome: a status code, or "refused" for a connect error.
 */
function fakeHttpByTransport({ onSocket = 200, onTcp = 200, token = "minted-token", sends = [] }) {
  return {
    get(url, options, callback) {
      const request = new EventEmitter();
      request.destroy = () => {};
      const transport = options.socketPath ? "socket" : "tcp";
      sends.push({ transport, url, socketPath: options.socketPath, secret: options.headers["X-Local-Secret"] });
      const outcome = transport === "socket" ? onSocket : onTcp;
      queueMicrotask(() => {
        if (outcome === "refused") {
          request.emit("error", new Error("ECONNREFUSED"));
          return;
        }
        const response = new EventEmitter();
        response.statusCode = outcome;
        response.resume = () => {};
        callback(response);
        if (outcome === 200) {
          response.emit("data", JSON.stringify({ token }));
          response.emit("end");
        }
      });
      return request;
    },
  };
}

function v4Secret(home, value = "v4-secret") {
  return [listenerSecretPath(home, "5476", "127.0.0.1", path), value];
}

describe("mintLocalToken over the gateway's unix socket", () => {
  it("sends over the socket and asks nothing of the TCP listener", async () => {
    const sends = [];
    let confirmed = 0;
    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => SOCKET_HOME,
      path,
      // Only the v4 entry exists, so a TCP dial of `localhost` would be refused
      // for want of v6 coverage; the socket does not depend on that coverage.
      fs: fakeTree({ files: new Map([v4Secret(SOCKET_HOME)]) }),
      http: fakeHttpByTransport({ sends }),
      getuid: () => OWN_UID,
      confirmTcpListener: async () => { confirmed += 1; return true; },
    });

    assert.equal(token, "minted-token");
    assert.equal(confirmed, 0, "no TCP listener question on the socket");
    assert.deepEqual(sends, [{
      transport: "socket",
      url: "http://localhost:5476/api/token/local",
      socketPath: path.join(SOCKET_HOME, "dashboard-5476.sock"),
      secret: "v4-secret",
    }]);
  });

  it("dials the socket under the home's real path, not through a symlink", async () => {
    const realHome = path.resolve(path.sep, "data", "crew");
    const sends = [];
    await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => SOCKET_HOME,
      path,
      fs: fakeTree({
        realpath: (p) => (p === SOCKET_HOME ? realHome : p),
        files: new Map([v4Secret(SOCKET_HOME)]),
      }),
      http: fakeHttpByTransport({ sends }),
      getuid: () => OWN_UID,
    });

    assert.equal(sends[0].socketPath, path.join(realHome, "dashboard-5476.sock"));
  });

  it("accepts a sticky world-writable ancestor such as /tmp", () => {
    const parent = path.dirname(SOCKET_HOME);
    const fs = fakeTree({ dirs: new Map([[parent, dirStat(0, 0o41777)]]) });
    assert.equal(
      ownedSocketPath({ target: dialTarget("http://localhost:5476"), home: SOCKET_HOME, path, fs, getuid: () => OWN_UID }),
      path.join(SOCKET_HOME, "dashboard-5476.sock"),
    );
  });

  const parent = path.dirname(SOCKET_HOME);
  const refusals = [
    ["Windows", { isWindows: true }],
    ["no uid source", { getuid: null }],
    ["a group-writable home", { tree: { homeStat: dirStat(OWN_UID, 0o40770) } }],
    ["an other-writable home", { tree: { homeStat: dirStat(OWN_UID, 0o40702) } }],
    ["a home owned by another uid", { tree: { homeStat: dirStat(OTHER_UID, 0o40700) } }],
    ["a world-writable ancestor without the sticky bit", { tree: { dirs: new Map([[parent, dirStat(0, 0o40777)]]) } }],
    ["an ancestor owned by another uid", { tree: { dirs: new Map([[parent, dirStat(OTHER_UID, 0o40755)]]) } }],
    ["a socket owned by another uid", { tree: { socket: { uid: OTHER_UID, isSocket: true } } }],
    ["a regular file where the socket belongs", { tree: { socket: { uid: OWN_UID, isSocket: false } } }],
    ["no socket at all", { tree: { socket: null } }],
  ];
  for (const [label, { tree = {}, isWindows = false, getuid = () => OWN_UID }] of refusals) {
    it(`keeps the TCP path, with its listener check, for ${label}`, async () => {
      const sends = [];
      let confirmed = 0;
      const token = await mintLocalToken({
        backendUrl: "http://127.0.0.1:5476",
        resolveHome: () => SOCKET_HOME,
        path,
        fs: fakeTree({ ...tree, files: new Map([v4Secret(SOCKET_HOME)]) }),
        http: fakeHttpByTransport({ sends }),
        isWindows,
        getuid,
        confirmTcpListener: async () => { confirmed += 1; return true; },
      });

      assert.equal(token, "minted-token");
      assert.equal(confirmed, 1, "the TCP listener is confirmed first");
      assert.deepEqual(sends.map((s) => s.transport), ["tcp"]);
    });
  }

  it("falls back to TCP when nothing answers on the socket", async () => {
    const sends = [];
    let confirmed = 0;
    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => SOCKET_HOME,
      path,
      fs: fakeTree({ files: new Map([v4Secret(SOCKET_HOME)]) }),
      http: fakeHttpByTransport({ onSocket: "refused", sends }),
      getuid: () => OWN_UID,
      confirmTcpListener: async () => { confirmed += 1; return true; },
    });

    assert.equal(token, "minted-token");
    assert.equal(confirmed, 1);
    assert.deepEqual(sends.map((s) => s.transport), ["socket", "tcp"]);
  });

  it("takes a refusal on the socket as final", async () => {
    const sends = [];
    let confirmed = 0;
    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => SOCKET_HOME,
      path,
      fs: fakeTree({ files: new Map([v4Secret(SOCKET_HOME)]) }),
      http: fakeHttpByTransport({ onSocket: 403, sends }),
      getuid: () => OWN_UID,
      confirmTcpListener: async () => { confirmed += 1; return true; },
    });

    assert.equal(token, "");
    assert.equal(confirmed, 0);
    assert.deepEqual(sends.map((s) => s.transport), ["socket"]);
  });

  it("walks every published candidate on the socket before giving up", async () => {
    const sends = [];
    let call = 0;
    const http = {
      get(url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        sends.push(options.headers["X-Local-Secret"]);
        const statusCode = call++ === 0 ? 403 : 200;
        queueMicrotask(() => {
          const response = new EventEmitter();
          response.statusCode = statusCode;
          response.resume = () => {};
          callback(response);
          if (statusCode === 200) {
            response.emit("data", JSON.stringify({ token: "live-token" }));
            response.emit("end");
          }
        });
        return request;
      },
    };
    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => SOCKET_HOME,
      path,
      fs: fakeTree({
        files: new Map([
          v4Secret(SOCKET_HOME, "stale-secret"),
          [listenerSecretPath(SOCKET_HOME, "5476", "::1", path), "live-secret"],
        ]),
      }),
      http,
      getuid: () => OWN_UID,
    });

    assert.equal(token, "live-token");
    assert.deepEqual(sends, ["stale-secret", "live-secret"]);
  });

  it("reads no secret and sends nothing on TCP when the listener is not confirmed", async () => {
    const sends = [];
    const reads = [];
    const token = await mintLocalToken({
      backendUrl: "http://127.0.0.1:5476",
      resolveHome: () => SOCKET_HOME,
      path,
      fs: fakeTree({ socket: null, files: new Map([v4Secret(SOCKET_HOME)]), reads }),
      http: fakeHttpByTransport({ sends }),
      getuid: () => OWN_UID,
      confirmTcpListener: async () => false,
    });

    assert.equal(token, "");
    assert.deepEqual(sends, []);
    assert.deepEqual(reads, []);
  });

  it("has one send site, and only this module sends the secret", () => {
    // Every send goes through `requestLocalToken`, whose callers are the socket
    // branch (after `ownedSocketPath`) and the TCP branch (after the listener
    // confirmation and the coverage test). A second literal would be a send that
    // bypasses both.
    const electronDir = path.join(__dirname, "..");
    const source = nodeFs.readFileSync(path.join(electronDir, "local-token.js"), "utf8");
    assert.equal(source.match(/"X-Local-Secret"/g).length, 1);
    const tokenSources = nodeFs.readFileSync(
      path.join(electronDir, "runtime", "gateway", "token-sources.js"),
      "utf8",
    );
    assert.doesNotMatch(tokenSources, /["']X-Local-Secret["']/);
  });

  it("mints over a real unix socket in an owner-only home", { skip: process.platform === "win32" }, async (t) => {
    const base = nodeFs.mkdtempSync(path.join(nodeOs.tmpdir(), "kc-sock-"));
    t.after(() => nodeFs.rmSync(base, { recursive: true, force: true }));
    const home = path.join(base, "crew");
    nodeFs.mkdirSync(path.join(home, "run"), { recursive: true, mode: 0o700 });
    nodeFs.chmodSync(home, 0o700);
    nodeFs.chmodSync(base, 0o700);
    nodeFs.writeFileSync(listenerSecretPath(home, "5476", "127.0.0.1", path), "real-secret\n", { mode: 0o600 });
    const target = dialTarget("http://localhost:5476");
    const socketPath = path.join(nodeFs.realpathSync(home), dashboardSocketName("5476"));

    const seen = [];
    const server = nodeHttp.createServer((req, res) => {
      seen.push({ url: req.url, secret: req.headers["x-local-secret"], host: req.headers.host });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ token: "socket-token" }));
    });
    await new Promise((resolve) => server.listen(socketPath, resolve));
    t.after(() => new Promise((resolve) => server.close(resolve)));

    const proven = ownedSocketPath({ target, home, path, fs: nodeFs, getuid: () => process.getuid() });
    if (!proven) {
      t.skip("this host's temp directory ancestry is not owner-protected");
      return;
    }
    const token = await mintLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => home,
      path,
      fs: nodeFs,
      http: nodeHttp,
      getuid: () => process.getuid(),
      confirmTcpListener: async () => { throw new Error("TCP must not be consulted"); },
    });

    assert.equal(token, "socket-token");
    assert.deepEqual(seen, [{ url: "/api/token/local", secret: "real-secret", host: "localhost:5476" }]);
  });
});

describe("dashboardSocketName", () => {
  it("matches the name the gateway binds", () => {
    assert.equal(dashboardSocketName("5476"), "dashboard-5476.sock");
  });
});
