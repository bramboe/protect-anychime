"""Network safety tests. All commands, DHCP processes and links are simulated."""
import json
import shlex
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from network import CommandRunner, NetworkManager, NetworkError, handle_dhcp, _load, _write


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.links = [{"ifname": "eth0", "ifindex": 2, "address": "00:11:22:33:44:55"}]
        self.addresses = {"eth0": [{"local": "10.23.45.10", "prefixlen": 24}]}
        self.routes = []
        self.rules = []
        self.processes = {}
        self.environments = []
        self.deny = False
        self.nat_rules = []
        self.nft_tables = {}
        self.nft_fail = False

    def run(self, args):
        self.calls.append(args)
        if args[:2] == ["ip", "-j"]:
            if "link" in args:
                return json.dumps(self.links)
            if "addr" in args:
                names = [args[-1]] if "dev" in args else list(self.addresses)
                return json.dumps([{"ifname": n, "addr_info": self.addresses.get(n, [])} for n in names])
            if "route" in args:
                return json.dumps(self.routes)
            if "rule" in args:
                return json.dumps(self.rules)
        if self.deny:
            raise NetworkError("Operation not permitted")
        if args[:4] == ['nft', '-j', 'list', 'tables']:
            return json.dumps({'nftables': [{'table': {'family': 'ip', 'name': name}} for name in self.nft_tables]})
        if args[:5] == ['nft', '-j', 'list', 'table', 'ip']:
            return json.dumps({'nftables': self.nft_tables[args[-1]]})
        if args[:2] == ['nft', '-j'] and '-f' in args:
            commands = json.loads(Path(args[-1]).read_text())['nftables']
            if self.nft_fail:
                raise NetworkError('Simulated nft transaction rejection')
            if '-c' in args:
                return ''
            for command in commands:
                if 'delete' in command:
                    del self.nft_tables[command['delete']['table']['name']]
                else:
                    item = command['add']
                    if 'table' in item:
                        self.nft_tables[item['table']['name']] = [item]
                    else:
                        value = next(iter(item.values()))
                        self.nft_tables[value['table']].append(item)
            return ''
        if args[:5] == ['iptables','-t','nat','-S','PREROUTING']:
            return '\n'.join(shlex.join(['-A','PREROUTING',*rule]) for rule in self.nat_rules)
        if args[:6] == ['iptables','-t','nat','-I','PREROUTING','1']:
            self.nat_rules.insert(0,args[6:]);return ''
        if args[:5] == ['iptables','-t','nat','-D','PREROUTING']:
            self.nat_rules.remove(args[5:]);return ''
        if args[:3] == ["ip", "link", "add"]:
            nic = args[args.index("name") + 1]
            self.links.append({"ifname": nic, "ifindex": 10, "link_index": 2,
                               "address": args[args.index("address") + 1], "mtu": int(args[args.index("mtu") + 1]),
                               "linkinfo": {"info_kind": "macvlan", "info_data": {"mode": "bridge"}}})
        elif args[:3] == ["ip", "link", "set"] and "alias" in args:
            next(v for v in self.links if v["ifname"] == args[4])["ifalias"] = args[-1]
        elif args[:3] == ["ip", "link", "set"] and "mtu" in args:
            next(v for v in self.links if v["ifname"] == args[4])["mtu"] = int(args[-1])
        elif args[:3] == ["ip", "link", "set"] and args[-1] == "up":
            next(v for v in self.links if v["ifname"] == args[4])["flags"] = ["UP"]
        elif args[:3] == ["ip", "link", "del"]:
            self.links = [v for v in self.links if v["ifname"] != args[-1]]
            self.addresses.pop(args[-1], None)
        elif args[:3] == ["ip", "-4", "addr"]:
            nic = args[args.index("dev") + 1]
            ip, prefix = args[4].split("/")
            if args[3] == "replace":
                self.addresses[nic] = [{"local": ip, "prefixlen": int(prefix)}]
            elif args[3] == "del":
                self.addresses[nic] = []
        elif args[:3] == ["ip", "-4", "route"]:
            dst = args[4]
            self.routes = [v for v in self.routes if v["dst"] != dst]
            if args[3] == "replace":
                route = {"dst": dst, "dev": args[args.index("dev") + 1]}
                if "via" in args:
                    route["gateway"] = args[args.index("via") + 1]
                self.routes.append(route)
        elif args[:3] == ["ip", "-4", "rule"]:
            if args[3] == "add":
                self.rules.append({"priority": int(args[args.index("priority") + 1]),
                                   "src": args[args.index("from") + 1], "table": int(args[-1])})
            else:
                self.rules = []
        return ""

    def spawn(self, args, env):
        self.calls.append(args)
        self.environments.append(env)
        pid = 100 + len(self.processes)
        self.processes[pid] = args
        return pid

    def matches(self, pid, args):
        return self.processes.get(pid) == args

    def terminate(self, pid, args):
        if self.matches(pid, args):
            del self.processes[pid]


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "network"
        self.runner = FakeRunner()
        self.manager = NetworkManager(self.directory, "10.23.45.10", "10.23.45.1", self.runner)
        self.manager._parent = lambda: ("eth0", "10.23.45.0/24")
        self.key = "media_player.office"
        self.mac = "02AABBCCDDEE"

    def tearDown(self):
        self.temp.cleanup()

    def start(self):
        result = self.manager.ensure(self.key, self.mac)
        self.assertEqual(result["state"], "pending", result)
        directory = self.manager._directory(self.key)
        return directory, _load(directory / "config.json")

    def lease(self, directory, config, event="bound", **fields):
        env = {"interface": config["interface"], "ip": "10.23.45.40",
               "subnet": "255.255.255.0", "router": "10.23.45.1", "lease": "3600", **fields}
        handle_dhcp(directory / "config.json", event, env, self.runner)

    def test_constructor_and_unstarted_status_have_no_side_effects(self):
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.manager.status(self.key)["state"], "pending")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.manager.stop(self.key)["state"], "pending")
        self.assertFalse(self.directory.exists())

    def test_dhcp_lifecycle_is_idempotent_and_does_not_touch_primary(self):
        directory, config = self.start()
        self.assertLessEqual(len(config["interface"]), 15)
        self.assertGreater(config["priority"], 0)
        self.assertLess(config["priority"], 32766)
        self.assertEqual(self.runner.links[-1]["mtu"], 1279)
        self.assertEqual(directory.stat().st_mode & 0o077, 0)
        self.assertEqual((directory / "config.json").stat().st_mode & 0o077, 0)
        self.assertEqual(set(self.runner.environments[0]), {"PATH", "ANYCHIME_NETWORK_CONFIG"})
        self.assertIn("-f", next(iter(self.runner.processes.values())))
        self.lease(directory, config)
        result = self.manager.status(self.key)
        self.assertEqual(result["state"], "ready", result)
        self.assertEqual(result["ip"], "10.23.45.40")
        before = len(self.runner.calls)
        self.manager.ensure(self.key, self.mac)
        self.assertTrue(all(call[:2] == ["ip", "-j"] for call in self.runner.calls[before:]))
        self.assertEqual(len(self.runner.environments), 1)
        self.lease(directory, config, "renew")
        self.assertEqual(len(self.runner.rules), 1)
        self.assertEqual(self.manager.stop(self.key)["state"], "pending")
        self.assertEqual(self.manager.stop(self.key)["state"], "pending")
        self.assertEqual(self.runner.routes, [])
        self.assertEqual(self.runner.rules, [])
        self.assertEqual([v["ifname"] for v in self.runner.links], ["eth0"])
        self.assertEqual(self.runner.addresses["eth0"], [{"local": "10.23.45.10", "prefixlen": 24}])
        for args in self.runner.calls:
            if args[:3] == ["ip", "-4", "addr"] and args[3] != "show":
                self.assertIn(config["interface"], args)
                if args[3] == "replace":
                    self.assertIn("noprefixroute", args)
            if args[:3] == ["ip", "-4", "route"]:
                self.assertIn("table", args)
                self.assertNotIn("main", args)
            self.assertNotIn("resolv.conf", " ".join(args))

    def test_renew_changes_ip_and_reports_once(self):
        directory, config = self.start()
        self.lease(directory, config)
        self.manager.status(self.key)
        self.lease(directory, config, "renew", ip="10.23.45.41")
        result = self.manager.ensure(self.key, self.mac)
        self.assertTrue(result["ip_changed"])
        self.assertEqual(result["previous_ip"], "10.23.45.40")
        self.assertEqual(result["ip"], "10.23.45.41")
        self.assertFalse(self.manager.status(self.key)["ip_changed"])
        self.assertEqual(self.runner.rules[0]["src"], "10.23.45.41/32")

    def test_invalid_lease_never_mutates_network(self):
        directory, config = self.start()
        bad = [{"ip": "10.23.45.10"}, {"ip": "10.23.45.1"}, {"ip": "8.8.8.8"},
               {"ip": "127.0.0.5"}, {"ip": "10.23.45.255"}, {"subnet": "255.0.0.0"},
               {"subnet": "255.255.255.255"}, {"router": "192.168.2.1"},
               {"router": "10.23.45.40"}, {"lease": "0"}, {"lease": "garbage"},
               {"interface": "eth0"}, {"router": ""}]
        for fields in bad:
            with self.subTest(fields=fields):
                before = len(self.runner.calls)
                with self.assertRaises(ValueError):
                    self.lease(directory, config, **fields)
                self.assertTrue(all(call[:2] == ["ip", "-j"] for call in self.runner.calls[before:]))
        self.assertIsNone(_load(directory / "lease.json"))

    def test_existing_local_address_is_rejected(self):
        directory, config = self.start()
        self.runner.addresses["some_other_link"] = [{"local": "10.23.45.40", "prefixlen": 24}]
        with self.assertRaisesRegex(NetworkError, "another local"):
            self.lease(directory, config)

    def test_same_namespace_named_parent_can_obtain_lease_and_be_reused(self):
        directory, config = self.start()
        link = self.runner.links[-1]
        link.pop("link_index")
        link["link"] = "eth0"
        self.lease(directory, config)
        self.assertEqual(self.manager.ensure(self.key, self.mac)["state"], "ready")
        self.assertEqual(self.manager.stop(self.key)["state"], "pending")

    def test_parent_metadata_conflicts_and_namespace_are_rejected_without_changes(self):
        self.start()
        original = dict(self.runner.links[-1])
        variants = [
            {"link": "another0"},
            {"link": "eth0", "link_index": 999},
            {"link": "eth0", "link_netnsid": 0},
            {"link_index": None},
        ]
        for metadata in variants:
            with self.subTest(metadata=metadata):
                self.runner.links[-1] = {**original, **metadata}
                before = len(self.runner.calls)
                self.assertEqual(self.manager.ensure(self.key, self.mac)["state"], "error")
                self.assertEqual(self.manager.stop(self.key)["state"], "error")
                self.assertTrue(all(call[:2] == ["ip", "-j"] for call in self.runner.calls[before:]))

    def test_foreign_link_is_never_reused_or_deleted(self):
        directory, config = self.start()
        self.runner.links[-1]["address"] = "02:aa:bb:cc:dd:ef"
        before = len(self.runner.calls)
        self.assertEqual(self.manager.ensure(self.key, self.mac)["state"], "error")
        self.assertEqual(self.manager.stop(self.key)["state"], "error")
        self.assertTrue(all(call[:2] == ["ip", "-j"] for call in self.runner.calls[before:]))

    def test_foreign_route_or_rule_is_never_overwritten(self):
        directory, config = self.start()
        for foreign in ("route", "rule"):
            with self.subTest(foreign=foreign):
                self.runner.routes = [{"dst": "default", "dev": "eth0"}] if foreign == "route" else []
                self.runner.rules = [{"priority": config["priority"], "src": "all", "table": 123}] if foreign == "rule" else []
                before = len(self.runner.calls)
                with self.assertRaisesRegex(NetworkError, "another owner"):
                    self.lease(directory, config)
                self.assertTrue(all(call[:2] == ["ip", "-j"] for call in self.runner.calls[before:]))

    def test_stop_refuses_unknown_routes_even_without_a_lease(self):
        self.start()
        self.runner.routes = [{"dst": "default", "dev": "eth0"}]
        before = len(self.runner.calls)
        self.assertEqual(self.manager.stop(self.key)["state"], "error")
        self.assertTrue(all(call[:2] == ["ip", "-j"] for call in self.runner.calls[before:]))
        self.assertEqual(len(self.runner.links), 2)

    def test_no_lease_is_fabricated_on_missing_permissions(self):
        self.runner.deny = True
        result = self.manager.ensure(self.key, self.mac)
        self.assertEqual(result["state"], "permission_required", result)
        self.assertIsNone(result["ip"])
        self.assertFalse(self.runner.environments)

    def test_expired_and_deconfigured_leases_are_pending(self):
        directory, config = self.start()
        self.lease(directory, config)
        lease = _load(directory / "lease.json")
        lease["expires_at"] = time.time() - 1
        _write(directory / "lease.json", lease)
        self.assertEqual(self.manager.status(self.key)["state"], "pending")
        self.lease(directory, config, "deconfig")
        self.assertIsNone(_load(directory / "lease.json"))
        self.assertEqual(self.runner.rules, [])
        self.assertEqual(self.runner.routes, [])

    def test_partial_configuration_never_becomes_ready(self):
        directory, config = self.start()
        self.runner.deny = True
        with self.assertRaises(NetworkError):
            self.lease(directory, config)
        self.assertFalse(_load(directory / "lease.json")["configured"])
        self.assertEqual(self.manager.status(self.key)["state"], "pending")

    def test_changed_identity_is_rejected(self):
        self.start()
        self.assertEqual(self.manager.ensure(self.key, "02AABBCCDDEF")["state"], "error")

    def test_private_directory_refuses_shared_permissions(self):
        self.directory.mkdir(mode=0o755)
        self.assertEqual(self.manager.ensure(self.key, self.mac)["state"], "error")
        self.assertEqual(self.runner.calls, [])

    def test_ipv4_mtu_is_set_only_on_our_child_before_up(self):
        _, config = self.start()
        calls = self.runner.calls
        mtu = ["ip", "link", "set", "dev", config["interface"], "mtu", "1279"]
        up = ["ip", "link", "set", "dev", config["interface"], "up"]
        self.assertLess(calls.index(mtu), calls.index(up))
        self.assertFalse(any("sysctl" in args for args in calls))
        self.assertFalse(any("mtu" in args and "eth0" in args and "set" in args for args in calls))

    def test_real_runner_matches_only_exact_foreground_busybox_command(self):
        runner = CommandRunner()
        args = ["/bin/busybox", "udhcpc", "-f", "-i", "ac123", "-s", "/app/dhcp_hook.py"]
        encoded = b"\0".join(a.encode() for a in args) + b"\0"
        with patch("network.Path.read_bytes", return_value=encoded):
            self.assertTrue(runner.matches(123, args))
            self.assertFalse(runner.matches(0, args))
            self.assertFalse(runner.matches(1, args))
            self.assertFalse(runner.matches(123, [*args[:-1], "/other/hook.py"]))
        with patch("network.Path.read_bytes", return_value=b""):
            self.assertFalse(runner.matches(123, args))

    def test_corrupted_mtu_and_missing_policy_are_never_ready(self):
        directory, config = self.start()
        self.lease(directory, config)
        self.runner.links[-1]["mtu"] = 1500
        self.assertEqual(self.manager.status(self.key)["state"], "error")
        self.runner.links[-1]["mtu"] = 1279
        self.runner.rules = []
        self.assertEqual(self.manager.status(self.key)["state"], "pending")

    def test_missing_table_is_treated_as_empty(self):
        original = self.runner.run
        def run(args):
            if args[:5] == ["ip", "-j", "-4", "route", "show"]:
                raise NetworkError("Error: ipv4: FIB table does not exist.")
            return original(args)
        self.runner.run = run
        self.assertEqual(self.manager.ensure(self.key, self.mac)["state"], "pending")


if __name__ == "__main__":
    unittest.main()

class ManagementPortTests(unittest.TestCase):
    setUp=NetworkTests.setUp
    tearDown=NetworkTests.tearDown
    start=NetworkTests.start
    lease=NetworkTests.lease
    def ready(self):
        directory,config=self.start();self.lease(directory,config)
        return directory,config

    def test_primary_also_gets_a_dedicated_dhcp_address(self):
        self.key='primary';directory,config=self.ready()
        self.assertEqual(self.manager.status(self.key)['ip'],'10.23.45.40')
        self.assertNotEqual(self.manager.status(self.key)['ip'],self.manager.primary_ip)

    def test_forward_is_limited_to_owned_console_and_address_before_docker(self):
        directory,config=self.ready()
        self.manager.redirect_management(self.key,'10.23.45.40',41000)
        objects=next(iter(self.runner.nft_tables.values()))
        self.assertEqual(objects[1]['chain']['prio'],-110)
        expr=objects[2]['rule']['expr']
        self.assertEqual([v['match']['right'] for v in expr[:-1]],['10.23.45.1','10.23.45.40',8080])
        self.assertEqual(expr[-1],{'dnat':{'addr':'10.23.45.40','port':41000}})
        self.assertFalse(any(call[0]=='iptables' for call in self.runner.calls))

    def test_restart_replaces_owned_table_and_stop_preserves_unrelated_tables(self):
        directory,config=self.ready()
        self.runner.nft_tables['docker-bridges']=[{'foreign':True}]
        self.manager.redirect_management(self.key,'10.23.45.40',41000)
        self.manager.redirect_management(self.key,'10.23.45.40',42000)
        self.assertEqual(len(self.runner.nft_tables),2)
        self.assertEqual(self.manager.stop(self.key)['state'],'pending')
        self.assertEqual(self.runner.nft_tables,{'docker-bridges':[{'foreign':True}]})

    def test_tampered_rule_and_unowned_address_are_rejected(self):
        directory,config=self.ready()
        for address,port in [('10.23.45.10',41000),('10.23.45.40',8080),('10.23.45.40',True)]:
            with self.assertRaises(NetworkError):self.manager.redirect_management(self.key,address,port)
        self.assertEqual(self.runner.nft_tables,{})
        self.manager.redirect_management(self.key,'10.23.45.40',41000)
        objects=next(iter(self.runner.nft_tables.values()))
        objects[2]['rule']['expr'][1]['match']['right']='10.23.45.99'
        self.assertEqual(self.manager.stop(self.key)['state'],'error')
        self.assertEqual(len(self.runner.nft_tables),1)

    def test_legacy_formats_are_validated_and_migrated_after_nft_installation(self):
        directory,config=self.ready()
        for interface_bound,dnat in [(True,False),(False,False),(False,True)]:
            with self.subTest(interface_bound=interface_bound,dnat=dnat):
                rule=self.manager._management_rule(config,'10.23.45.40',41000,interface_bound,dnat)
                pairs=list(zip(rule[::2],rule[1::2]))
                reordered=[part for pair in sorted(pairs,reverse=True) for part in pair]
                self.runner.nat_rules=[reordered]
                _write(directory/'management.json',{'ip':'10.23.45.40','port':41000,
                    'interface_bound':interface_bound,'dnat':dnat})
                self.manager.redirect_management(self.key,'10.23.45.40',42000)
                self.assertEqual(self.runner.nat_rules,[])
                self.assertEqual(len(self.runner.nft_tables),1)
                commit=max(i for i,c in enumerate(self.runner.calls) if c[:3]==['nft','-j','-f'])
                delete=max(i for i,c in enumerate(self.runner.calls) if c[:5]==['iptables','-t','nat','-D','PREROUTING'])
                self.assertLess(commit,delete)

    def test_owner_comment_cannot_authorise_changed_legacy_rule(self):
        directory,config=self.ready()
        rule=self.manager._management_rule(config,'10.23.45.40',41000)
        self.runner.nat_rules=[rule+['--random','1']]
        _write(directory/'management.json',{'ip':'10.23.45.40','port':41000,'interface_bound':False,'dnat':True})
        with self.assertRaises(NetworkError):self.manager._clear_legacy_management(directory,config)
        self.assertEqual(len(self.runner.nat_rules),1)

    def test_failed_validation_preserves_working_table_and_legacy_rule(self):
        directory,config=self.ready()
        self.manager.redirect_management(self.key,'10.23.45.40',41000)
        before=json.loads(json.dumps(self.runner.nft_tables))
        self.runner.nft_fail=True
        with self.assertRaises(NetworkError):self.manager.redirect_management(self.key,'10.23.45.40',42000)
        self.assertEqual(self.runner.nft_tables,before)
        self.runner.nft_fail=False
        self.assertEqual(self.manager.stop(self.key)['state'],'pending')
        self.assertEqual(self.runner.nft_tables,{})

    def test_crash_after_commit_can_recover_pending_state(self):
        directory,config=self.ready()
        self.manager.redirect_management(self.key,'10.23.45.40',41000)
        journal=_load(directory/'nft-management.json')
        _write(directory/'nft-management.json',{'active':None,'pending':journal['active']})
        self.runner.nft_fail=True
        with self.assertRaises(NetworkError):self.manager.redirect_management(self.key,'10.23.45.40',42000)
        self.runner.nft_fail=False
        self.assertEqual(self.manager.stop(self.key)['state'],'pending')

    def test_missing_kernel_support_preserves_legacy_rule(self):
        directory,config=self.ready()
        rule=self.manager._management_rule(config,'10.23.45.40',41000)
        self.runner.nat_rules=[rule]
        _write(directory/'management.json',{'ip':'10.23.45.40','port':41000,'interface_bound':False,'dnat':True})
        with patch('network.platform.release',return_value='4.17.0'):
            with self.assertRaisesRegex(NetworkError,'4.18'):
                self.manager.redirect_management(self.key,'10.23.45.40',42000)
        self.assertEqual(self.runner.nat_rules,[rule])

    def test_kernel_handles_are_ignored_but_additional_rules_are_not_owned(self):
        directory,config=self.ready()
        self.manager.redirect_management(self.key,'10.23.45.40',41000)
        objects=next(iter(self.runner.nft_tables.values()))
        for i,obj in enumerate(objects):next(iter(obj.values()))['handle']=i+1
        self.manager.redirect_management(self.key,'10.23.45.40',42000)
        next(iter(self.runner.nft_tables.values())).append({'rule':{'foreign':True}})
        self.assertEqual(self.manager.stop(self.key)['state'],'error')
