"""Explicit-start macvlan/DHCP backend. Import and construction do no network work.

Only owned links and their source-policy routes may be changed. CommandRunner is
injectable: tests must never require network privileges or start a DHCP client.
"""
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import subprocess
import time
import platform
import tempfile
from contextlib import contextmanager


class NetworkError(ValueError):
    pass


class CommandRunner:
    def __init__(self):
        self._children = {}

    def run(self, args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=8,
                                env={"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"})
        if result.returncode:
            raise NetworkError(result.stderr.strip()[:240] or "Network command failed")
        return result.stdout

    def spawn(self, args, env):
        child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        self._children[child.pid] = child
        return child.pid

    def matches(self, pid, args):
        try:
            pid = int(pid)
            if pid <= 1:
                return False
            child = self._children.get(pid)
            if child is not None and child.poll() is not None:
                self._children.pop(pid, None)
                return False
            # BusyBox's normal multicall path shifts argv pointers internally;
            # it does not rewrite the kernel's original argument string bytes.
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            return [v.decode() for v in cmd if v] == args
        except (OSError, ValueError, UnicodeError):
            return False

    def terminate(self, pid, args):
        if not self.matches(pid, args):
            return
        os.kill(pid, signal.SIGTERM)
        # Bound shutdown; the process's hook must finish before deleting its link.
        for _ in range(20):
            if not self.matches(pid, args):
                return
            time.sleep(.05)
        if self.matches(pid, args):
            os.kill(pid, signal.SIGKILL)
            child = self._children.pop(pid, None)
            if child is not None:
                child.wait(timeout=2)


def _json(runner, *args):
    return json.loads(runner.run(["ip", "-j", *args]) or "[]")


def _write(path, data):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _load(path, default=None):
    try:
        if Path(path).is_symlink():
            raise NetworkError("Symlink in private network state")
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


@contextmanager
def _locked(directory):
    fd = os.open(Path(directory) / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _private_ip(value):
    address = ipaddress.IPv4Address(value)
    # is_private also includes non-LAN special ranges in Python; require RFC1918.
    if not any(address in ipaddress.IPv4Network(net) for net in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
        raise NetworkError("A private LAN IPv4 address is required")
    return str(address)


def _mac(value):
    compact = value.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", compact) or int(compact[:2], 16) & 3 != 2:
        raise NetworkError("A stable locally administered unicast MAC is required")
    return ":".join(compact[i:i + 2] for i in range(0, 12, 2))


def _validate_config(config):
    digest = hashlib.sha256(config["node_key"].encode()).hexdigest()
    if (config["interface"] != "ac" + digest[:12]
            or config["alias"] != "anychime:" + digest
            or config["table"] != 100000 + int(digest[:8], 16) % 1000000000
            or config["priority"] != 1000 + int(digest[8:16], 16) % 30000):
        raise NetworkError("Invalid network ownership metadata")
    _mac(config["mac"])
    _private_ip(config["primary_ip"])
    ipaddress.IPv4Address(config["controller_ip"])
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", config["parent"]):
        raise NetworkError("Invalid parent interface")


def _owned_link(runner, config, missing_ok=False):
    links = _json(runner, "-d", "link", "show")
    found = next((v for v in links if v.get("ifname") == config["interface"]), None)
    if found is None:
        if missing_ok:
            return None
        raise NetworkError("Owned network interface is missing")
    info = found.get("linkinfo", {})
    parent = next((v for v in links if v.get("ifname") == config["parent"]), {})
    # iproute2 prints same-namespace parents as "link": "eth0".
    # Numeric link_index is used by other versions / namespace representations.
    # Reject ambiguous or cross-namespace metadata instead of trusting a name alone.
    named = found.get("link")
    indexed = found.get("link_index")
    parent_matches = bool(parent) and (
        (named is not None and named == config["parent"] and
         (indexed is None or indexed == parent.get("ifindex")))
        or (named is None and type(indexed) is int and indexed == parent.get("ifindex")))
    checks = {
        "device address": found.get("address", "").lower() == config["mac"],
        "ownership label": found.get("ifalias") == config["alias"],
        "interface type": info.get("info_kind") == "macvlan",
        "bridge mode": info.get("info_data", {}).get("mode") == "bridge",
        "parent interface": parent_matches and "link_netnsid" not in found,
    }
    failed = [label for label, matches in checks.items() if not matches]
    if failed:
        raise NetworkError("Chime network interface could not be verified (" + ", ".join(failed) + ").")
    return found


def _routes(runner, config):
    try:
        return _json(runner, "-4", "route", "show", "table", str(config["table"]))
    except NetworkError as exc:
        if "FIB table does not exist" in str(exc):
            return []
        raise


def _routing_owned(runner, config, lease):
    """Never replace/delete a route or rule from another owner, even on collision."""
    routes = _routes(runner, config)
    rules = _json(runner, "-4", "rule", "show")
    allowed = {"default", (lease or {}).get("network")}
    for route in routes:
        if (not lease or route.get("dev") != config["interface"]
                or route.get("dst", "default") not in allowed
                or route.get("gateway") not in (None, lease["router"])):
            raise NetworkError("Routing table is in use by another owner")
    for rule in rules:
        if (str(rule.get("table")) == str(config["table"])
                or rule.get("priority") == config["priority"]):
            if (not lease or str(rule.get("table")) != str(config["table"])
                    or rule.get("priority") != config["priority"]
                    or rule.get("src") not in (lease["ip"], lease["ip"] + "/32")
                    or any(key in rule for key in ("dst", "fwmark", "iif", "oif"))):
                raise NetworkError("Policy priority/table is in use by another owner")


def _remove_lease(runner, config, lease, link_exists=True):
    if not lease:
        return
    _routing_owned(runner, config, lease)
    rules = _json(runner, "-4", "rule", "show")
    for rule in rules:
        if str(rule.get("table")) == str(config["table"]):
            runner.run(["ip", "-4", "rule", "del", "priority", str(config["priority"]),
                        "from", lease["ip"] + "/32", "table", str(config["table"])])
    routes = _routes(runner, config)
    for route in routes:
        runner.run(["ip", "-4", "route", "del", route.get("dst", "default"),
                    "dev", config["interface"], "table", str(config["table"])])
    if link_exists:
        addresses = _json(runner, "-4", "addr", "show", "dev", config["interface"])
        if any(a.get("local") == lease["ip"] and a.get("prefixlen") == lease["prefix"]
               for entry in addresses for a in entry.get("addr_info", [])):
            runner.run(["ip", "-4", "addr", "del", lease["cidr"], "dev", config["interface"]])


def handle_dhcp(config_path, event, environ, runner=None):
    """udhcpc hook; no calls to a generic distro hook, DNS or main routing table."""
    runner = runner or CommandRunner()
    config_path = Path(config_path)
    with _locked(config_path.parent):
        config = _load(config_path)
        _validate_config(config)
        if environ.get("interface") != config["interface"]:
            raise NetworkError("DHCP interface does not match owned node")
        _owned_link(runner, config)
        lease_path = config_path.parent / "lease.json"
        old = _load(lease_path)
        if event in ("deconfig", "nak", "leasefail"):
            _remove_lease(runner, config, old)
            _write(lease_path, None)
            return
        if event not in ("bound", "renew"):
            return
        ip = _private_ip(environ.get("ip", ""))
        if ip in (config["primary_ip"], config["controller_ip"]):
            raise NetworkError("DHCP offered a protected primary/controller address")
        mask = environ.get("subnet", "")
        interface = ipaddress.IPv4Interface(f"{ip}/{mask}")
        network = interface.network
        if not 8 <= network.prefixlen <= 30:
            raise NetworkError("DHCP subnet must have a usable LAN prefix")
        # Require the same LAN as the original interface, not an arbitrary route.
        parent_network = ipaddress.IPv4Network(config["parent_network"])
        if network != parent_network:
            raise NetworkError("DHCP subnet differs from the primary LAN")
        routers = environ.get("router", "").split()
        if not routers:
            raise NetworkError("DHCP did not provide a router")
        router = _private_ip(routers[0])
        if (ipaddress.IPv4Address(router) not in network
                or router in (str(network.network_address), str(network.broadcast_address), ip)
                or ip in (str(network.network_address), str(network.broadcast_address))):
            raise NetworkError("DHCP address/router is outside the usable subnet")
        seconds = int(environ.get("lease", "0"))
        if not 1 <= seconds <= 0xFFFFFFFF:
            raise NetworkError("DHCP lease duration is missing or invalid")
        _routing_owned(runner, config, old)
        # Refuse an address already present on ANY interface, except our current lease.
        addresses = _json(runner, "-4", "addr", "show")
        for entry in addresses:
            if any(a.get("local") == ip for a in entry.get("addr_info", [])):
                if entry.get("ifname") != config["interface"] or not old or old["ip"] != ip:
                    raise NetworkError("DHCP address already belongs to another local interface")
        new = {"ip": ip, "prefix": network.prefixlen, "cidr": str(interface),
               "network": str(network), "router": router, "obtained_at": time.time(),
               "expires_at": time.time() + seconds, "previous_ip": (old or {}).get("ip")}
        # Persist intention before mutations, so interrupted operations can be cleaned up.
        if old and (old["ip"] != ip or old["router"] != router):
            _remove_lease(runner, config, old)
        _write(lease_path, new)
        try:
            runner.run(["ip", "-4", "addr", "replace", new["cidr"], "dev", config["interface"],
                        "noprefixroute", "valid_lft", str(seconds), "preferred_lft", str(seconds)])
            runner.run(["ip", "-4", "route", "replace", new["network"], "dev", config["interface"],
                        "src", ip, "table", str(config["table"])])
            runner.run(["ip", "-4", "route", "replace", "default", "via", router,
                        "dev", config["interface"], "src", ip, "table", str(config["table"])])
            rules = _json(runner, "-4", "rule", "show")
            if not any(str(v.get("table")) == str(config["table"]) for v in rules):
                runner.run(["ip", "-4", "rule", "add", "priority", str(config["priority"]),
                            "from", ip + "/32", "table", str(config["table"])])
            new["configured"] = True
            _write(lease_path, new)
            (config_path.parent / "hook-error.json").unlink(missing_ok=True)
        except Exception:
            # A pending lease is never reported ready after a partial hook failure.
            new["configured"] = False
            _write(lease_path, new)
            raise


class NftManagement:
    """One owned table per chime, independent of Docker's iptables backend.

    A single nft transaction replaces the complete owned table. No global table,
    Docker chain, host policy or alternative selection is ever modified.
    """
    def __init__(self, runner, directory, config):
        self.runner, self.directory, self.config = runner, directory, config
        self.name = 'ac_' + hashlib.sha256(config['node_key'].encode()).hexdigest()
        self.path = directory / 'nft-management.json'

    def objects(self, state):
        # Reuse the strict address/port validation used by legacy cleanup.
        NetworkManager._management_rule(self.config, state['ip'], state['port'])
        table = {'family': 'ip', 'name': self.name}
        chain = {'family': 'ip', 'table': self.name, 'name': 'management',
                 'type': 'nat', 'hook': 'prerouting', 'prio': -110, 'policy': 'accept'}
        expressions = []
        for protocol, field, value in [('ip', 'saddr', self.config['controller_ip']),
                                        ('ip', 'daddr', state['ip']), ('tcp', 'dport', 8080)]:
            expressions.append({'match': {'op': '==', 'left': {'payload': {
                'protocol': protocol, 'field': field}}, 'right': value}})
        expressions.append({'dnat': {'addr': state['ip'], 'port': state['port']}})
        rule = {'family': 'ip', 'table': self.name, 'chain': 'management',
                'expr': expressions, 'comment': self.config['alias'] + ':management'}
        return [{'table': table}, {'chain': chain}, {'rule': rule}]

    def existing(self, journal):
        tables = json.loads(self.runner.run(['nft', '-j', 'list', 'tables']))['nftables']
        if not any(v.get('table', {}).get('family') == 'ip' and
                   v['table'].get('name') == self.name for v in tables):
            return False
        result = json.loads(self.runner.run(['nft', '-j', 'list', 'table', 'ip', self.name]))['nftables']
        actual = []
        for item in result:
            if 'metainfo' in item:
                continue
            clean = json.loads(json.dumps(item))
            for value in clean.values():
                value.pop('handle', None)
            actual.append(clean)
        candidates = [journal.get('active'), journal.get('pending')] if journal else []
        for state in candidates:
            if state and actual == self.objects(state):
                return state
        raise NetworkError('Chime nftables ownership changed; refusing to replace or remove it')

    def transaction(self, commands):
        fd, name = tempfile.mkstemp(prefix='nft-', suffix='.json', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'nftables': commands}, stream)
            # -c asks the kernel to validate capabilities and the entire batch
            # without committing. The following batch commits atomically.
            self.runner.run(['nft', '-j', '-c', '-f', name])
            self.runner.run(['nft', '-j', '-f', name])
        finally:
            Path(name).unlink(missing_ok=True)

    def install(self, ip, port):
        version = re.match(r'^(\d+)\.(\d+)', platform.release())
        if not version or tuple(map(int, version.groups())) < (4, 18):
            raise NetworkError('Chime networking requires Linux 4.18+ with nftables NAT support')
        state = {'ip': ip, 'port': port}
        objects = self.objects(state)
        previous = _load(self.path, {}) or {}
        exists = self.existing(previous)
        commands = ([{'delete': {'table': {'family': 'ip', 'name': self.name}}}] if exists else [])
        commands += [{'add': item} for item in objects]
        # Retain both formats through a crash: an unsuccessful atomic batch leaves
        # the previous table; a successful one leaves the pending table.
        _write(self.path, {'active': exists or None, 'pending': state})
        self.transaction(commands)
        _write(self.path, {'active': state})

    def clear(self):
        journal = _load(self.path)
        if not journal:
            return
        if self.existing(journal):
            self.transaction([{'delete': {'table': {'family': 'ip', 'name': self.name}}}])
        _write(self.path, None)


class NetworkManager:
    def __init__(self, state_dir, primary_ip, controller_ip, runner=None):
        self.directory = Path(state_dir).absolute()
        self.primary_ip = _private_ip(primary_ip)
        self.controller_ip = str(ipaddress.IPv4Address(controller_ip))
        self.runner = runner or CommandRunner()
        self._reported = {}

    def _directory(self, node_key):
        if not isinstance(node_key, str) or not 1 <= len(node_key) <= 512:
            raise NetworkError("Invalid node key")
        return self.directory / hashlib.sha256(node_key.encode()).hexdigest()

    @staticmethod
    def _management_rule(config, ip, port, interface_bound=False, dnat=True):
        _validate_config(config)
        ip=_private_ip(ip)
        if type(port) is not int or not 1024<=port<=65535 or port==8080:
            raise NetworkError('Invalid private chime listener port')
        if ip in (config['primary_ip'],config['controller_ip']):
            raise NetworkError('A dedicated chime address is required')
        if type(interface_bound) is not bool or type(dnat) is not bool:
            raise NetworkError('Invalid management rule ownership state')
        # macvlan ingress can traverse the parent interface at this hook. Match
        # the exact owned DHCP address instead of assuming a netfilter iifname.
        target=['-j','DNAT','--to-destination',ip+':'+str(port)] if dnat else ['-j','REDIRECT','--to-ports',str(port)]
        return (['-i',config['interface']] if interface_bound else [])+['-s',config['controller_ip']+'/32','-d',ip+'/32',
            '-p','tcp','-m','tcp','--dport','8080','-m','comment','--comment',
            config['alias']+':management',*target]

    def _clear_management(self, directory, config):
        NftManagement(self.runner, directory, config).clear()
        self._clear_legacy_management(directory, config)

    def _clear_legacy_management(self, directory, config):
        state=_load(directory/'management.json')
        if not state:return
        rule=self._management_rule(config,state['ip'],state['port'],state.get('interface_bound',True),state.get('dnat',False))
        marker=config['alias']+':management'
        lines=self.runner.run(['iptables','-t','nat','-S','PREROUTING']).splitlines()
        for line in lines:
            parts=shlex.split(line)
            if '--comment' not in parts:continue
            position=parts.index('--comment')
            if position+1>=len(parts) or parts[position+1]!=marker:continue
            if (parts[:2]!=['-A','PREROUTING'] or
                    self._management_signature(parts[2:])!=self._management_signature(rule)):
                raise NetworkError('Chime port-forward ownership changed; refusing to remove it')
            self.runner.run(['iptables','-t','nat','-D','PREROUTING',*parts[2:]])
        _write(directory/'management.json',None)

    @staticmethod
    def _management_signature(arguments):
        """iptables prints base options in its own order, not insertion order."""
        allowed={'-i','-s','-d','-p','-m','--dport','--comment','-j','--to-ports','--to-destination'}
        if len(arguments)%2:
            raise NetworkError('Unrecognised chime management rule format')
        seen=set();values=[]
        for option,value in zip(arguments[::2],arguments[1::2]):
            if option not in allowed or (option in seen and option!='-m'):
                raise NetworkError('Unrecognised chime management rule option')
            if option=='-m' and value not in ('tcp','comment'):
                raise NetworkError('Unrecognised chime management rule match')
            seen.add(option)
            if option in ('-s','-d'):
                value=str(ipaddress.IPv4Network(value,strict=False))
            values.append((option,value))
        return tuple(sorted(values))

    def redirect_management(self, node_key, ip, port):
        """Expose 8080 only on an owned chime IP; never bind the host's 8080."""
        directory=self._directory(node_key)
        with _locked(directory):
            config=_load(directory/'config.json')
            _validate_config(config)
            _owned_link(self.runner,config)
            lease=_load(directory/'lease.json')
            if not lease or lease.get('ip')!=ip:
                raise NetworkError('Chime port-forward address does not match its owned DHCP lease')
            # Install and validate the replacement before touching legacy rules.
            # nft's priority -110 precedes the standard Docker dstnat hook -100,
            # including Docker using iptables-legacy, iptables-nft or native nft.
            try:
                NftManagement(self.runner, directory, config).install(ip, port)
            except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                raise NetworkError('Chime networking needs accessible nftables and NET_ADMIN') from exc
            self._clear_legacy_management(directory,config)

    def _parent(self):
        routes = Path("/proc/net/route").read_text().splitlines()[1:]
        candidates = []
        for line in routes:
            values = line.split()
            if len(values) >= 8 and values[1] == "00000000" and int(values[3], 16) & 3 == 3:
                candidates.append((int(values[6]), values[0]))
        if not candidates:
            raise NetworkError("No physical LAN default interface was found")
        parent = min(candidates)[1]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", parent):
            raise NetworkError("Invalid physical interface name")
        sys = Path("/sys/class/net") / parent
        if not (sys / "device").exists() or (sys / "wireless").exists():
            raise NetworkError("Default interface is not a supported physical wired LAN interface")
        entries = _json(self.runner, "-4", "addr", "show", "dev", parent)
        for entry in entries:
            for address in entry.get("addr_info", []):
                if address.get("local") == self.primary_ip:
                    return parent, str(ipaddress.IPv4Network(
                        f"{self.primary_ip}/{address['prefixlen']}", strict=False))
        raise NetworkError("Primary address is not on the physical default interface")

    def _args(self, directory, config):
        return ["/bin/busybox", "udhcpc", "-f", "-i", config["interface"], "-s",
                str(Path(__file__).with_name("dhcp_hook.py").absolute()),
                "-p", str(directory / "udhcpc.pid"), "-t", "3", "-T", "3", "-A", "20"]

    def _result(self, state, reason, ip=None, **extra):
        return {"state": state, "ip": ip, "reason": reason, **extra}

    def ensure(self, node_key, mac):
        try:
            directory = self._directory(node_key)
            for path in (self.directory, directory):
                if path.is_symlink():
                    raise NetworkError("Symlink in private network directory")
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.stat().st_mode & 0o077:
                    raise NetworkError("Network state directory must be private")
            with _locked(directory):
                mac = _mac(mac)
                config = _load(directory / "config.json")
                if config:
                    _validate_config(config)
                    if (config["mac"] != mac or config["node_key"] != node_key
                            or config["primary_ip"] != self.primary_ip
                            or config["controller_ip"] != self.controller_ip):
                        raise NetworkError("Network identity/configuration changed; refusing to reuse")
                else:
                    parent, subnet = self._parent()
                    digest = directory.name
                    table = 100000 + int(digest[:8], 16) % 1000000000
                    config = {"node_key": node_key, "mac": mac, "interface": "ac" + digest[:12],
                              "alias": "anychime:" + digest, "parent": parent,
                              "parent_network": subnet, "table": table,
                              "priority": 1000 + int(digest[8:16], 16) % 30000,
                              "primary_ip": self.primary_ip, "controller_ip": self.controller_ip}
                    _validate_config(config)
                    _write(directory / "config.json", config)
                link = _owned_link(self.runner, config, missing_ok=True)
                lease = _load(directory / "lease.json")
                _routing_owned(self.runner, config, lease)
                if link is None:
                    # Existing table/rules must have been cleaned before creating a new link.
                    if lease:
                        _remove_lease(self.runner, config, lease, link_exists=False)
                        _write(directory / "lease.json", None)
                    self.runner.run(["ip", "link", "add", "link", config["parent"], "name",
                                     config["interface"], "address", mac, "mtu", "1279",
                                     "type", "macvlan", "mode", "bridge"])
                    self.runner.run(["ip", "link", "set", "dev", config["interface"], "alias", config["alias"]])
                # Linux refuses IPv6 device initialization below its minimum MTU
                # (1280). This IPv4-only link needs no writable /proc/sys or global
                # sysctl. Source rules before main also satisfy strict rp_filter.
                if link is None or link.get("mtu") != 1279:
                    self.runner.run(["ip", "link", "set", "dev", config["interface"], "mtu", "1279"])
                if link is None or "UP" not in link.get("flags", []):
                    self.runner.run(["ip", "link", "set", "dev", config["interface"], "up"])
                args = self._args(directory, config)
                process = _load(directory / "process.json", {})
                if not self.runner.matches(process.get("pid", 0), args):
                    env = {"PATH": "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                           "ANYCHIME_NETWORK_CONFIG": str(directory / "config.json")}
                    pid = self.runner.spawn(args, env)
                    _write(directory / "process.json", {"pid": pid})
            return self.status(node_key)
        except (NetworkError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            reason = str(exc)[:240]
            permission = any(term in reason.lower() for term in ("operation not permitted", "permission denied", "not permitted", "read-only file system"))
            return self._result("permission_required" if permission else "error", reason)

    def status(self, node_key=None):
        if node_key is None:
            if not self.directory.exists():
                return []
            return [self.status(_load(p)["node_key"]) for p in self.directory.glob("*/config.json")]
        try:
            directory = self._directory(node_key)
            config = _load(directory / "config.json")
            if not config:
                return self._result("pending", "Network has not been started")
            _validate_config(config)
            lease = _load(directory / "lease.json")
            if not lease or not lease.get("configured") or lease["expires_at"] <= time.time():
                error = _load(directory / "hook-error.json", {})
                return self._result("pending", error.get("reason", "Waiting for a valid DHCP lease"), interface=config["interface"])
            link = _owned_link(self.runner, config)
            if link.get("mtu") != 1279:
                raise NetworkError("Owned IPv4 interface has an unexpected MTU")
            addresses = _json(self.runner, "-4", "addr", "show", "dev", config["interface"])
            if not any(a.get("local") == lease["ip"] for e in addresses for a in e.get("addr_info", [])):
                return self._result("pending", "DHCP address is not present", interface=config["interface"])
            _routing_owned(self.runner, config, lease)
            routes = _routes(self.runner, config)
            rules = _json(self.runner, "-4", "rule", "show")
            if (not {"default", lease["network"]}.issubset({v.get("dst", "default") for v in routes})
                    or not any(str(v.get("table")) == str(config["table"])
                               and v.get("src") in (lease["ip"], lease["ip"] + "/32") for v in rules)):
                return self._result("pending", "DHCP source routing is incomplete", interface=config["interface"])
            process = _load(directory / "process.json", {})
            if not self.runner.matches(process.get("pid", 0), self._args(directory, config)):
                return self._result("pending", "DHCP client is not running", interface=config["interface"])
            previous = self._reported.get(node_key)
            self._reported[node_key] = lease["ip"]
            return self._result("ready", "DHCP lease configured", lease["ip"], interface=config["interface"],
                                previous_ip=previous, ip_changed=bool(previous and previous != lease["ip"]))
        except (NetworkError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            return self._result("error", str(exc)[:240])

    def stop(self, node_key=None):
        if node_key is None:
            if not self.directory.exists():
                return []
            return [self.stop(_load(p)["node_key"]) for p in self.directory.glob("*/config.json")]
        try:
            directory = self._directory(node_key)
            config = _load(directory / "config.json")
            if not config:
                return self._result("pending", "Network is already stopped")
            _validate_config(config)
            args = self._args(directory, config)
            process = _load(directory / "process.json", {})
            self.runner.terminate(process.get("pid", 0), args)
            with _locked(directory):
                link = _owned_link(self.runner, config, missing_ok=True)
                self._clear_management(directory,config)
                lease = _load(directory / "lease.json")
                _routing_owned(self.runner, config, lease)
                _remove_lease(self.runner, config, lease, link_exists=bool(link))
                if link:
                    self.runner.run(["ip", "link", "del", "dev", config["interface"]])
                _write(directory / "lease.json", None)
                _write(directory / "process.json", {})
                self._reported.pop(node_key, None)
            return self._result("pending", "Network stopped")
        except (NetworkError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            return self._result("error", str(exc)[:240])

    def forget(self, node_key):
        """Remove owned stopped network state so a new identity gets a fresh MAC."""
        directory=self._directory(node_key)
        if self.directory.is_symlink() or directory.is_symlink() or not directory.resolve().is_relative_to(self.directory.resolve()):
            raise NetworkError('Unsafe network cleanup path')
        result=self.stop(node_key)
        if result['state']=='error':
            raise NetworkError('Owned chime network could not be stopped for cleanup')
        if directory.exists():shutil.rmtree(directory)
        self._reported.pop(node_key,None)
