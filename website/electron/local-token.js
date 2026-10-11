"use strict";

/**
 * The literal-loopback origin of `backendUrl` and the port it addresses.
 *
 * One parse serves both, because the credential is a function of the dial
 * target: resolving the origin here and the port somewhere else would let a
 * caller authenticate for one listener while dialing another.
 *
 * `URL.port` is "" on a scheme-default port, so the default is spelled out:
 * an empty port would name a credential file no gateway ever wrote.
 *
 * @param {string} backendUrl
 * @returns {{origin: string, port: string} | null} null when the URL is not a
 *   literal `http:` loopback origin, which is the only target a local secret
 *   may be sent to.
 */

/**
 * The loopback families a dialed host can land on, and the bind addresses that
 * cover each one.
 *
 * A family is covered by its own literal and by its wildcard. `::` is counted as
 * v6 ONLY: whether a v6 wildcard also accepts v4-mapped connections depends on
 * the host's `IPV6_V6ONLY` setting, which this side cannot establish, so counting
 * it for v4 would be a guess in the permissive direction.
 */
const LOOPBACK_FAMILY_BINDS = Object.freeze({
  v4: Object.freeze(["127.0.0.1", "0.0.0.0"]),
  v6: Object.freeze(["::1", "::"]),
});

/** Hostnames that name a SET of listeners rather than one. */
const AMBIGUOUS_LOOPBACK_NAMES = Object.freeze(["localhost", "kirocrew.localhost"]);

/**
 * What a `backendUrl` actually dials: its origin, its port, and every loopback
 * family the host can resolve to.
 *
 * The families are the whole point. `localhost` resolves to BOTH loopback
 * families on an ordinary host, so dialing it can land on either listener; a
 * literal address lands on exactly one. Naming the families here, rather than
 * rewriting the host to a literal, is what lets the caller ask the only question
 * that makes an ambiguous name safe: does this gateway hold all of them?
 *
 * @param {string} backendUrl
 * @returns {{origin: string, port: string, families: string[]} | null} null when
 *   the URL is not an `http:` loopback target, the only kind a local secret may
 *   be sent to.
 */
function dialTarget(backendUrl) {
  try {
    const url = new URL(backendUrl);
    if (url.protocol !== "http:") return null;
    const host = url.hostname;
    let families;
    if (AMBIGUOUS_LOOPBACK_NAMES.includes(host)) {
      families = ["v4", "v6"];
    } else if (host === "127.0.0.1") {
      families = ["v4"];
    } else if (host === "[::1]" || host === "::1") {
      families = ["v6"];
    } else {
      return null;
    }
    return { origin: url.origin, port: url.port || "80", families };
  } catch {
    return null;
  }
}

/**
 * The secrets it is safe to send to `target`, or `null` meaning refuse.
 *
 * THE PREDICATE, and the whole security argument of this change now rests here.
 * A gateway publishes `run/gateway-<port>-<address>.secret` for each address it
 * bound, so the set of those files IS the evidence of which listeners on this
 * port are this gateway's. The rule:
 *
 *   every family the dialed host can reach must be covered by an entry.
 *
 * For a literal host that is one family, which is the old behaviour. For an
 * ambiguous name it is both, and that is what makes the name safe to dial: if
 * the gateway holds v4 and v6 on this port, then whichever one the resolver
 * picks, the party reached is the gateway that published the credential. A
 * co-resident cannot be on either, because the gateway is.
 *
 * A single missing family is a refusal, not a narrowing. If the gateway bound
 * only v4 and something else holds `[::1]:<port>`, dialing `localhost` may reach
 * that squatter, so no secret goes out at all and the caller falls through to the
 * remote-token path and then the token prompt. That is the same fail-closed cost
 * a v6-bound gateway already pays, and it is paid by refusing rather than by
 * guessing which listener answered.
 *
 * Reading only local disk keeps the peer out of it: a process that merely holds
 * the port, whatever it presents itself as, is never consulted and never
 * believed.
 *
 * @returns {string[] | null} secrets to try in order, or null to refuse
 */
function listenerSecretsFor({ target, home, path, fs }) {
  const perFamily = [];
  for (const family of target.families) {
    const candidates = [];
    for (const bindAddress of LOOPBACK_FAMILY_BINDS[family]) {
      let secret = "";
      try {
        secret = fs
          .readFileSync(listenerSecretPath(home, target.port, bindAddress, path), "utf8")
          .trim();
      } catch {
        // A missing/unreadable entry simply does not cover this family.
      }
      if (!secret) continue;
      // EVERY present entry is a candidate, not just the first. `clear_marker`
      // runs only on a graceful shutdown, so a crashed generation leaves its
      // entry behind; stopping at that stale secret would spend the walk and
      // refuse a gateway that is actually reachable through the wildcard entry
      // beside it. A refused candidate must not end the walk.
      if (!candidates.includes(secret)) candidates.push(secret);
    }
    // One uncovered family the dialed host can reach is enough to refuse: the
    // resolver may hand us exactly that listener.
    if (candidates.length === 0) return null;
    perFamily.push(candidates);
  }
  // THE INTERSECTION, and it is what makes coverage a fact about the LIVE
  // generation rather than about which files happen to exist.
  //
  // Presence alone fails OPEN. Nothing deletes a sidecar except a graceful
  // shutdown, so a SIGKILLed generation that held both families leaves its v6
  // entry behind; a co-resident then takes that address, this gateway restarts
  // and binds v4 only, and a presence test would call v6 "covered" by the DEAD
  // generation's file and send the LIVE secret to the squatter.
  //
  // What distinguishes them is already on disk: one generation writes the SAME
  // secret under every address it bound, and each start mints a fresh one. So a
  // secret appearing under an address in EVERY family the dialed host can reach
  // is proof that one generation holds them all, and a stale file from another
  // generation carries a different value and drops out of the intersection.
  //
  // A literal host has ONE family, so the intersection is that family's own
  // candidate list and this changes nothing for it -- including the stale-entry
  // walk above.
  const [first, ...rest] = perFamily;
  const shared = first.filter((secret) => rest.every((family) => family.includes(secret)));
  if (shared.length === 0) return null;
  return shared;
}

/**
 * Filename-safe spelling of a bind address, identical to the writer's.
 *
 * `:` is legal in an IPv6 literal and illegal in a Windows filename, so the
 * gateway rewrites it to `_` when it publishes (`run_marker.encode_bind_address`).
 * A reader that builds the raw spelling looks for a file that never exists, which
 * fails as a REFUSAL rather than as an error: the family reads as uncovered and
 * the mint declines. So this mapping is not cosmetic, and it is pinned against the
 * writer's own output by a test that names the literal file both sides produce.
 *
 * @param {string} bindAddress
 * @returns {string}
 */
function encodeBindAddress(bindAddress) {
  return String(bindAddress).replace(/:/g, "_");
}

/**
 * Path of the gateway credential for the listener at `bindAddress` on `port`.
 *
 * A gateway publishes its own in-memory credential, once it has bound, as
 * `run/gateway-<port>-<address>.secret`: mode 0600 inside an owner-only `run/`
 * directory. Reading that file answers "may a secret go to whatever answers this
 * address and port?" out of local disk state, so nothing is asked of the peer --
 * a process that merely holds the port, whatever it presents itself as, is never
 * consulted and never believed.
 *
 * The name carries the address because a port number identifies a SET of
 * listeners. Keyed by port alone, a gateway bound to the v6 loopback and a
 * tunnel's local end on v4 share one entry, and the app would read the former's
 * credential and send it to the latter.
 *
 * @param {string} home data home whose `config.json` governs this launch
 * @param {string} port port being dialed
 * @param {string} bindAddress address whose listener published the credential
 * @param {object} path node:path (injected)
 * @returns {string}
 */
function listenerSecretPath(home, port, bindAddress, path) {
  return path.join(home, "run", `gateway-${port}-${encodeBindAddress(bindAddress)}.secret`);
}

/** Group-write and other-write permission bits. */
const GROUP_OTHER_WRITE = 0o022;
/** The sticky bit: only an entry's owner (or root) may rename or unlink it. */
const STICKY = 0o1000;

/**
 * Name of the gateway's unix socket for `port`, identical to the writer's
 * (`dashboard_socket_name`): `dashboard-<port>.sock` directly in the data home.
 *
 * @param {string} port
 * @returns {string}
 */
function dashboardSocketName(port) {
  return `dashboard-${port}.sock`;
}

/**
 * The gateway's unix socket for `target`, when the filesystem itself proves that
 * only this user can have created it; otherwise `null`.
 *
 * TCP loopback authenticates nobody: a port is held by whoever bound it last, so
 * every check made before a TCP send is a snapshot the listener can change after.
 * A socket entry is different. Creating, replacing or renaming it needs write
 * permission on the directory that holds it, and that is a standing property of
 * the directory rather than a moment's observation. So the proof is:
 *
 *   - the data home is a directory owned by this uid with no group/other write;
 *   - every ancestor is owned by this uid or root, and is not group/other
 *     writable unless sticky (a sticky directory lets nobody else move an entry
 *     this uid or root owns, and the entry below is checked to be one);
 *   - the entry is a socket owned by this uid.
 *
 * Then no other uid can have put that socket there or can swap it before the
 * connect, which closes the window against every principal the secret excludes.
 * A process running as this same uid is not one of them: it can already read the
 * secret file.
 *
 * The walk runs over the home's REAL path and the returned path is built from it,
 * so a symlink in the configured path cannot redirect the dial after the check.
 *
 * Any failure -- Windows, no uid source, a missing socket (an older gateway, or a
 * path too long for the socket address), an fs call that throws, a permission
 * that fails the rule -- answers `null`, and the caller keeps today's TCP path
 * with all of its checks.
 *
 * @returns {string | null}
 */
function ownedSocketPath({ target, home, path, fs, isWindows, getuid }) {
  if (isWindows || typeof getuid !== "function") return null;
  try {
    const uid = getuid();
    if (typeof uid !== "number") return null;
    const realHome = fs.realpathSync(home);
    const homeStat = fs.statSync(realHome);
    if (!homeStat.isDirectory() || homeStat.uid !== uid) return null;
    if (homeStat.mode & GROUP_OTHER_WRITE) return null;
    let dir = realHome;
    for (;;) {
      const parent = path.dirname(dir);
      if (parent === dir) break;
      const parentStat = fs.statSync(parent);
      if (parentStat.uid !== uid && parentStat.uid !== 0) return null;
      if ((parentStat.mode & GROUP_OTHER_WRITE) && !(parentStat.mode & STICKY)) return null;
      dir = parent;
    }
    const socketPath = path.join(realHome, dashboardSocketName(target.port));
    const socketStat = fs.lstatSync(socketPath);
    if (!socketStat.isSocket() || socketStat.uid !== uid) return null;
    return socketPath;
  } catch {
    return null;
  }
}

/**
 * Every credential published for `target.port` in this home, for the socket
 * transport.
 *
 * On the socket the recipient is established by `ownedSocketPath`, so the
 * per-family coverage that `listenerSecretsFor` demands of a TCP dial answers a
 * question this transport does not ask. Every candidate names a listener this
 * home's gateway bound on this port; a stale one from a crashed generation is
 * refused by the live gateway and the walk moves on.
 *
 * @returns {string[]} secrets to try in order (possibly empty)
 */
function socketSecretsFor({ target, home, path, fs }) {
  const secrets = [];
  for (const binds of Object.values(LOOPBACK_FAMILY_BINDS)) {
    for (const bindAddress of binds) {
      let secret = "";
      try {
        secret = fs
          .readFileSync(listenerSecretPath(home, target.port, bindAddress, path), "utf8")
          .trim();
      } catch {
        // A missing/unreadable entry contributes no candidate.
      }
      if (secret && !secrets.includes(secret)) secrets.push(secret);
    }
  }
  return secrets;
}

/**
 * Ask the gateway at `dialedOrigin` to exchange the local secret for a token.
 *
 * `dialedOrigin` is the origin exactly as the caller's configured URL spells it --
 * usually `http://localhost:<port>`. With `socketPath` the request travels over
 * that unix socket instead, and the origin only supplies the request's Host. A
 * secret may go out only after the caller's check for the transport in use:
 * `ownedSocketPath` for the socket, and for TCP the coverage test
 * (`listenerSecretsFor`) plus the caller's listener confirmation. This function
 * performs no check of its own and must not be called without them.
 *
 * @returns {Promise<{token: string, answered: boolean}>} `answered` is false when
 *   no HTTP response arrived at all (refused connect, timeout).
 */
async function requestLocalToken(http, dialedOrigin, secret, socketPath = null) {
  if (!dialedOrigin) return { token: "", answered: false };
  const options = { headers: { "X-Local-Secret": secret }, timeout: 5000 };
  if (socketPath) options.socketPath = socketPath;
  return new Promise((resolve) => {
    const req = http.get(
      `${dialedOrigin}/api/token/local`,
      options,
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          resolve({ token: "", answered: true });
          return;
        }
        let data = "";
        res.on("error", () => resolve({ token: "", answered: true }));
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          let token = "";
          try { token = JSON.parse(data).token || ""; } catch { token = ""; }
          resolve({ token, answered: true });
        });
      },
    );
    req.on("error", () => resolve({ token: "", answered: false }));
    req.on("timeout", () => { req.destroy(); resolve({ token: "", answered: false }); });
  });
}

/**
 * A dashboard token minted against the gateway that owns the dialed listener.
 *
 * The credential sent is the one that gateway published for its own listener,
 * identified by the address AND the port that were dialed. Neither the home-wide
 * `.local_secret` nor a port-keyed entry is a second place to look. The shared
 * file holds one slot per data home on a last-writer-wins basis, and a port-keyed
 * entry names every listener sharing that port number, so either can hold a
 * credential belonging to a gateway this call never reached. Sending that to
 * whoever answers here would surrender a credential which authenticates
 * elsewhere. An entry keyed by the dialed listener cannot escalate, because the
 * only listener it authenticates against is the one it was just sent to.
 *
 * An absent entry therefore denies rather than widens. A port no local gateway
 * bound on this address, an `ssh -L` forward's local end among them, whether or
 * not a v6-bound gateway holds the same port number, has no credential to read,
 * so the caller falls through to the remote-token path and then to the token
 * prompt.
 *
 * A REFUSED entry is not the end of the walk either. `clear_marker` runs only on
 * a graceful shutdown, so a crashed gateway leaves its entry behind, and a stale
 * exact-address entry sitting beside the live wildcard one would otherwise spend
 * the single attempt and report no token while a working credential was never
 * tried. Continuing costs nothing that matters: a dead generation's secret
 * authenticates against no listener at all, and every candidate names a listener
 * on the address being dialed, so the walk never reaches a party this call did
 * not reach.
 *
 * The token comes back ALONE, and every request that carries it keeps naming the
 * configured backend URL. The token is a bearer -- whoever receives it holds
 * dashboard authority until it expires -- and the host that URL spells may be
 * `localhost`, which names both loopback families, so something has to decide
 * that the name reaches nobody else. That decision is the coverage test above,
 * taken BEFORE the secret leaves: no token is minted unless this gateway holds
 * every family the dialed host resolves to. A second spelling to address would
 * not add to it and would cost something real -- the shell's renderer keys its
 * storage on the origin, so handing callers a rewritten one splits the settings
 * of every window addressed with it away from the dashboard's own.
 *
 * The socket comes first. When `ownedSocketPath` proves the gateway's unix socket
 * can only have been created by this user, the secret goes over it and no TCP
 * listener is involved at all. When the socket is unavailable, or nothing answers
 * on it, the mint falls back to TCP with every TCP check intact; a gateway that
 * answers on the socket and refuses every candidate is the same gateway TCP would
 * reach, so a refusal there is final.
 *
 * @param {object} deps
 * @param {boolean} [deps.isWindows] no socket transport on Windows
 * @param {() => number} [deps.getuid] this process's uid; without it the socket
 *        is never used
 * @param {() => Promise<boolean>} [deps.confirmTcpListener] the caller's own
 *        proof that the TCP listener is its gateway, awaited before any secret
 *        is read for a TCP send; `false` refuses the TCP branch
 * @returns {Promise<string>} the minted token, or `""` for a miss and for every
 *   refusal alike, so a caller treats an absent credential as one case.
 */
async function mintLocalToken({
  backendUrl,
  resolveHome,
  path,
  fs,
  http,
  isWindows = false,
  getuid = null,
  confirmTcpListener = null,
}) {
  const target = dialTarget(backendUrl);
  if (!target) return "";
  const home = resolveHome();

  const socketPath = ownedSocketPath({ target, home, path, fs, isWindows, getuid });
  if (socketPath) {
    let answered = false;
    for (const secret of socketSecretsFor({ target, home, path, fs })) {
      const result = await requestLocalToken(http, target.origin, secret, socketPath);
      if (result.token) return result.token;
      if (!result.answered) break;
      answered = true;
    }
    if (answered) return "";
  }

  if (confirmTcpListener && !(await confirmTcpListener())) return "";
  const secrets = listenerSecretsFor({ target, home, path, fs });
  // `null` is a REFUSAL, not a miss: some family this host resolves to has no
  // sidecar, so the gateway does not demonstrably hold every listener the name
  // can reach, and a co-resident could be holding one of them. Sending nothing
  // costs one explicit sign-in; sending the secret could cost the secret.
  if (!secrets) return "";
  for (const secret of secrets) {
    // `target.origin` is the URL's OWN origin, never rewritten -- that is why
    // the document's storage, and every comparison against the configured
    // string, stay where they are.
    const { token } = await requestLocalToken(http, target.origin, secret);
    if (token) return token;
  }
  return "";
}

module.exports = {
  mintLocalToken,
  encodeBindAddress,
  listenerSecretPath,
  dialTarget,
  listenerSecretsFor,
  ownedSocketPath,
  socketSecretsFor,
  dashboardSocketName,
  AMBIGUOUS_LOOPBACK_NAMES,
  LOOPBACK_FAMILY_BINDS,
};
