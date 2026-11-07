"""The custom firmware's Wi-Fi link in the emulator: joining networks, DHCP,
ping both ways and the access point mode, against the virtual network in
wifi_net.py attached to the BCM43143 model (printer profile)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_fw_bootloader import DUMP, _have_compiler  # noqa: E402

PROMPT = b'app> '
LONG = 1_500_000_000            # instruction budget for commands that wait on the network


@unittest.skipUnless(DUMP.exists(), 'dump.bin (HP stage-1) is required')
@unittest.skipUnless(_have_compiler(), 'no ARM C compiler available')
class WifiNetworkTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fw.build import build_all
        cls.tmp = tempfile.TemporaryDirectory()
        manifest = build_all(Path(cls.tmp.name) / 'net', app_version='1.0.0', app_sequence=1,
                             build_time=1_700_000_000)
        cls.flash = manifest['_flash_path'].read_bytes()
        # The hello example, to run from RAM over HTTP.
        cls.manifest_hello = build_all(Path(cls.tmp.name) / 'hello', app_version='0.1.0', app_sequence=1,
                                       build_time=1_700_000_200, write_flash=False, build_bl=False,
                                       app_src=ROOT / 'fw' / 'examples' / 'hello')
        # A newer application for the update over HTTP.
        cls.manifest_v2 = build_all(Path(cls.tmp.name) / 'net2', app_version='1.0.1', app_sequence=2,
                                    build_time=1_700_000_100, write_flash=False, build_bl=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def boot(self):
        from fw.harness import boot_to_app
        from wifi_net import VirtualWifiNetwork
        vp = boot_to_app(self.flash)
        net = VirtualWifiNetwork(vp.emu.board.sdio.card)
        return vp, net

    @staticmethod
    def run_until(vp, condition, budget=400_000_000, step=4_000_000):
        """Let the idle firmware run (its idle loop polls the chip) until condition() holds."""
        used = 0
        while not condition() and used < budget:
            vp.run_steps(step)
            used += step
        return condition()

    def test_station_join_dhcp_ping_and_leave(self):
        vp, net = self.boot()
        net.add_ap('Lab-WPA2', 'lab-passphrase-42', channel=11, rssi=-51)
        out = vp.command('wifi join Lab-WPA2 lab-passphrase-42', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: firmware says: wl0: Mar 17 2016 15:56:01 version 6.20.198.22', out)   # loaded on demand
        self.assertIn('wifi: joining "Lab-WPA2" (WPA2-PSK, handshake by the chip)', out)
        self.assertIn('wifi: connected to "Lab-WPA2"', out)
        self.assertIn('signal -51 dBm', out)
        self.assertIn('net: asking for an address (DHCP)', out)
        self.assertIn('net: address 192.168.50.100/24, gateway 192.168.50.1, lease 3600 s (DHCP)', out)
        # What the chip was told: AES, WPA2-PSK, its own supplicant, the passphrase, the events.
        self.assertIn('joined Lab-WPA2 (WPA2)', net.log)
        self.assertEqual((net.wsec, net.wpa_auth, net.sup_wpa), (4, 0x80, 1))
        self.assertEqual(net.passphrase, 'lab-passphrase-42')
        self.assertIn('dhcp server: discover -> offer 192.168.50.100', net.log)
        self.assertIn('dhcp server: request -> ack 192.168.50.100', net.log)
        self.assertFalse([line for line in net.log if 'not enabled' in line or 'bad' in line], net.log)
        # The printer pings the router ...
        out = vp.command('ping 192.168.50.1 3', PROMPT, max_instructions=LONG)
        self.assertIn('reply from 192.168.50.1: seq 0', out)
        self.assertIn('reply from 192.168.50.1: seq 2', out)
        self.assertIn('ping: 3 of 3 answered', out)
        self.assertEqual(net.echo_requests, [0, 1, 2])
        # ... and answers the router's pings from its idle loop.
        net.ping_printer(count=2)
        self.assertTrue(self.run_until(vp, lambda: len(net.echo_replies) >= 2), net.log[-5:])
        self.assertEqual(net.echo_replies, [0, 1])
        out = vp.command('net', PROMPT)
        self.assertIn('net: link up (station), address 192.168.50.100/24, gateway 192.168.50.1, DHCP bound', out)
        self.assertIn('net: arp 192.168.50.1', out)
        self.assertIn('echo answered 2', out)
        out = vp.command('wifi', PROMPT)
        self.assertIn('wifi: mode station, network "Lab-WPA2", signal -51 dBm', out)
        out = vp.command('wifi rssi', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: signal -51 dBm from "Lab-WPA2"', out)
        out = vp.command('wifi leave', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: left the network', out)
        self.assertIn('left Lab-WPA2', net.log)
        out = vp.command('ping 192.168.50.1 1', PROMPT)
        self.assertIn('ping: no address yet', out)
        self.assertEqual(vp.flash_writes(), [])

    def test_join_failures_are_explained(self):
        vp, net = self.boot()
        net.add_ap('Lab-WPA2', 'lab-passphrase-42')
        net.add_ap('Cafe-Open', None, channel=1, rssi=-70)
        out = vp.command('wifi join Lab-WPA2 not-the-passphrase', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: join failed (event 46, status 8, reason 12): the WPA2 handshake failed, '
                      'probably a wrong passphrase', out)
        self.assertNotIn('connected to', out)
        self.assertIn('join Lab-WPA2: wrong passphrase', net.log)
        out = vp.command('wifi join Nowhere-5G', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: join failed (event 0, status 3, reason 0): network not found', out)
        out = vp.command('wifi join Lab-WPA2 short', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: a WPA2 passphrase has 8..63 characters', out)
        out = vp.command('wifi', PROMPT)
        self.assertIn('wifi: mode idle (chip firmware running)', out)
        # An open network works without a passphrase.
        out = vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: joining "Cafe-Open" (open)', out)
        self.assertIn('wifi: connected to "Cafe-Open"', out)
        self.assertIn('net: address 192.168.50.100/24', out)
        self.assertEqual(net.wsec, 0)

    def test_link_lost_and_back(self):
        from wifi_net import E_LINK
        vp, net = self.boot()
        ap = net.add_ap('Lab-WPA2', 'lab-passphrase-42')
        out = vp.command('wifi join Lab-WPA2 lab-passphrase-42', PROMPT, max_instructions=LONG)
        self.assertIn('net: address 192.168.50.100/24', out)
        mark = len(vp.emu.uart)
        net._event(E_LINK, flags=0, addr=ap.bssid)                 # beacon loss
        self.assertTrue(self.run_until(vp, lambda: b'wifi: link lost' in vp.uart_bytes(mark)))
        out = vp.command('net', PROMPT)
        self.assertIn('net: link down', out)
        mark = len(vp.emu.uart)
        net._event(E_LINK, flags=1, addr=ap.bssid)                 # the chip found the network again
        self.assertTrue(self.run_until(vp, lambda: b'net: address 192.168.50.100/24' in vp.uart_bytes(mark)))
        self.assertIn('wifi: link back to "Lab-WPA2"', vp.uart_text(mark))
        out = vp.command('ping 192.168.50.1 1', PROMPT, max_instructions=LONG)
        self.assertIn('ping: 1 of 1 answered', out)

    def test_quoted_network_name_and_passphrase(self):
        vp, net = self.boot()
        net.add_ap('Home Net - 5', 'pass phrase with spaces')
        out = vp.command('wifi join "Home Net - 5" "pass phrase with spaces"', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: joining "Home Net - 5" (WPA2-PSK, handshake by the chip)', out)
        self.assertIn('wifi: connected to "Home Net - 5"', out)
        self.assertEqual(net.passphrase, 'pass phrase with spaces')

    def test_static_address(self):
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        out = vp.command('net ip 192.168.50.77 255.255.255.0 192.168.50.1', PROMPT)
        self.assertIn('static address stored', out)
        out = vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        self.assertIn('net: static address 192.168.50.77/24, gateway 192.168.50.1', out)
        self.assertNotIn('DHCP', out)
        out = vp.command('ping 192.168.50.1 2', PROMPT, max_instructions=LONG)
        self.assertIn('ping: 2 of 2 answered', out)
        out = vp.command('net dhcp', PROMPT, max_instructions=LONG)
        self.assertIn('net: asking for an address (DHCP)', out)

    def test_access_point_with_dhcp_server_and_ping(self):
        vp, net = self.boot()
        out = vp.command('wifi ap', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: starting access point "HPDJ-2001" on channel 6 (open)', out)
        self.assertIn('wifi: access point "HPDJ-2001" is up: join it and ping 192.168.4.1', out)
        self.assertIn('net: access point address 192.168.4.1/24, DHCP for .10 to .17', out)
        self.assertIn('access point HPDJ-2001 up on channel 6 (open)', net.log)
        start = len(vp.emu.uart)
        net.associate_client()
        self.assertTrue(self.run_until(vp, lambda: net.client_ip is not None), net.log[-5:])
        self.assertEqual(net.client_ip, bytes([192, 168, 4, 10]))
        self.assertTrue(self.run_until(vp, lambda: b'leased to' in vp.uart_bytes(start)))
        text = vp.uart_text(start)
        self.assertIn('wifi: station 02:00:c0:a8:04:42 joined', text)
        self.assertIn('net: 192.168.4.10 leased to 02:00:c0:a8:04:42', text)
        net.ping_printer(count=3)
        self.assertTrue(self.run_until(vp, lambda: len(net.echo_replies) >= 3), net.log[-5:])
        out = vp.command('ping 192.168.4.10 2', PROMPT, max_instructions=LONG)
        self.assertIn('ping: 2 of 2 answered', out)
        out = vp.command('net', PROMPT)
        self.assertIn('net: link up (access point), address 192.168.4.1/24', out)
        self.assertIn('net: lease 192.168.4.10 02:00:c0:a8:04:42', out)
        out = vp.command('wifi', PROMPT)
        self.assertIn('wifi: mode access point, network "HPDJ-2001", 1 stations', out)
        out = vp.command('wifi leave', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: access point stopped', out)
        # WPA2 access point on another channel: the chip's authenticator gets the passphrase.
        out = vp.command('wifi ap Printer-Lab lab-passphrase-42 11', PROMPT, max_instructions=LONG)
        self.assertIn('wifi: starting access point "Printer-Lab" on channel 11 (WPA2-PSK)', out)
        self.assertIn('access point Printer-Lab up on channel 11 (WPA2)', net.log)
        self.assertEqual((net.passphrase, net.sup_wpa, net.wpa_auth), ('lab-passphrase-42', 0, 0x80))
        out = vp.command('wifi off', PROMPT)
        self.assertIn('module power off', out)
        out = vp.command('net', PROMPT)
        self.assertIn('net: link down', out)

    def test_http_motor_server(self):
        import json
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        out = vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        self.assertIn('net: address 192.168.50.100/24', out)
        out = vp.command('server', PROMPT)
        self.assertIn('server: listening at http://192.168.50.100/ (port 80', out)

        # Fetch the HTML control panel (multi-segment TCP response)
        net.start_http_get('/')
        self.assertTrue(self.run_until(vp, lambda: net.http_done), net.log[-5:])
        resp = net.http_response.decode('utf-8', errors='replace')
        self.assertIn('HTTP/1.1 200 OK', resp)
        self.assertIn('HP DeskJet 2620', resp)
        self.assertIn('Motor B', resp)

        # Drive Motor B forward via HTTP API (auto-initializes MCASIC)
        net.start_http_get('/api/motor?act=move&m=b&dir=fwd&pwm=350&ms=200')
        self.assertTrue(self.run_until(vp, lambda: net.http_done), net.log[-5:])
        body = net.http_response.decode('utf-8').split('\r\n\r\n', 1)[1]
        data = json.loads(body)
        self.assertTrue(data['ok'])
        self.assertEqual(data['inited'], 1)
        self.assertEqual(data['active_motor'], 1)
        self.assertEqual(data['active_mode'], 1)
        self.assertEqual(data['active_pwm'], 350)
        self.assertEqual(data['mcasic']['ctl'][1], '115e')

        # Stop all motors via HTTP API
        net.start_http_get('/api/motor?act=stop')
        self.assertTrue(self.run_until(vp, lambda: net.http_done), net.log[-5:])
        body = net.http_response.decode('utf-8').split('\r\n\r\n', 1)[1]
        data = json.loads(body)
        self.assertTrue(data['ok'])
        self.assertEqual(data['active_motor'], -1)
        self.assertEqual(data['mcasic']['ctl'][1], '0000')

    def http(self, vp, net, method, path, body=b'', budget=600_000_000):
        import json
        net.start_http_request(method, path, body)
        self.assertTrue(self.run_until(vp, lambda: net.http_done, budget=budget), net.log[-5:])
        head, _, payload = net.http_response.decode('utf-8', errors='replace').partition('\r\n\r\n')
        return head, (json.loads(payload) if payload.startswith('{') else payload)

    def test_rejoin_in_the_background_after_the_link_is_lost(self):
        from wifi_net import E_LINK
        vp, net = self.boot()
        ap = net.add_ap('Lab-WPA2', 'lab-passphrase-42')
        out = vp.command('wifi join Lab-WPA2 lab-passphrase-42', PROMPT, max_instructions=LONG)
        self.assertIn('net: address 192.168.50.100/24', out)
        # The access point goes away and the chip does not find it again by itself.
        del net.aps[b'Lab-WPA2']
        net.joined = None
        mark = len(vp.emu.uart)
        net._event(E_LINK, flags=0, addr=ap.bssid)
        self.assertTrue(self.run_until(vp, lambda: b'next try in 6 s' in vp.uart_bytes(mark), budget=3_000_000_000))
        text = vp.uart_text(mark)
        self.assertIn('wifi: will rejoin "Lab-WPA2" in 3 s', text)
        self.assertIn('wifi: rejoining in the background (attempt 1)', text)
        self.assertIn('network not found', text)
        # The shell keeps answering between attempts.
        out = vp.command('wifi', PROMPT)
        self.assertIn('wifi: auto rejoin on for "Lab-WPA2"; links lost 1, background attempts 1, rejoined 0', out)
        # It is back: the next attempt joins and gets an address again.
        net.aps[b'Lab-WPA2'] = ap
        mark = len(vp.emu.uart)
        self.assertTrue(self.run_until(vp, lambda: b'net: address 192.168.50.100/24' in vp.uart_bytes(mark),
                                       budget=4_000_000_000))
        self.assertIn('wifi: connected to "Lab-WPA2"', vp.uart_text(mark))
        out = vp.command('wifi', PROMPT)
        self.assertIn('rejoined 1', out)
        out = vp.command('ping 192.168.50.1 1', PROMPT, max_instructions=LONG)
        self.assertIn('ping: 1 of 1 answered', out)
        # "wifi leave" means it: no more attempts.
        vp.command('wifi leave', PROMPT, max_instructions=LONG)
        mark = len(vp.emu.uart)
        vp.run_steps(400_000_000)
        self.assertNotIn(b'rejoining', vp.uart_bytes(mark))

    def test_mdns_answers_for_the_printer_name(self):
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        out = vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        self.assertIn('net: address 192.168.50.100/24', out)
        addr = bytes([192, 168, 50, 100])
        # Two announcements after the address arrived.
        self.assertTrue(self.run_until(vp, lambda: len(net.mdns_answers) >= 2, budget=2_000_000_000))
        self.assertEqual(net.mdns_answers[0], ('deskjet.local', addr, 5353))
        net.mdns_answers.clear()
        net.mdns_query('DeskJet.local')                         # case does not matter
        self.assertTrue(self.run_until(vp, lambda: net.mdns_answers))
        self.assertEqual(net.mdns_answers, [('deskjet.local', addr, 5353)])
        net.mdns_answers.clear()
        net.mdns_query('deskjet.local', legacy=True)           # one-shot resolver: unicast answer
        self.assertTrue(self.run_until(vp, lambda: net.mdns_answers))
        self.assertEqual(net.mdns_answers, [('deskjet.local', addr, 40000)])
        net.mdns_answers.clear()
        net.mdns_query('other-printer.local')
        vp.run_steps(100_000_000)
        self.assertEqual(net.mdns_answers, [])
        out = vp.command('net name lab-printer', PROMPT)
        self.assertIn('net: mDNS name lab-printer.local', out)
        net.mdns_answers.clear()
        net.mdns_query('lab-printer.local')
        self.assertTrue(self.run_until(vp, lambda: ('lab-printer.local', addr, 5353) in net.mdns_answers))

    def test_web_status_log_and_shell(self):
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        head, page = self.http(vp, net, 'GET', '/')
        self.assertIn('200 OK', head)
        for part in ('Sensors', 'Shell', 'Update application', '/api/log'):
            self.assertIn(part, page)
        _, st = self.http(vp, net, 'GET', '/api/status')
        self.assertEqual(st['host'], 'deskjet.local')
        self.assertEqual(st['ip'], '192.168.50.100')
        self.assertEqual(st['slot'], 'A')
        self.assertEqual(st['sens']['door'] in (0, 1), True)
        self.assertEqual(st['pos'], [0, 0, 0])
        _, log = self.http(vp, net, 'GET', '/api/log?from=0')
        self.assertIn('HPDJ app v1.0.0', log['text'])               # the boot banner is in the log
        self.assertIn('wifi: connected to "Cafe-Open"', log['text'])
        head, res = self.http(vp, net, 'GET', '/api/cmd?c=version')
        self.assertIn('403', head)
        _, res = self.http(vp, net, 'GET', '/api/cmd?token=hpdj2620&c=echo+hello%20web')
        self.assertTrue(res['ok'])
        self.assertEqual(res['out'], 'hello web\n')
        _, res = self.http(vp, net, 'GET', '/api/cmd?token=hpdj2620&c=net')
        self.assertIn('net: link up (station), address 192.168.50.100/24', res['out'])
        self.assertIn('mDNS name deskjet.local', res['out'])
        head, res = self.http(vp, net, 'GET', '/api/cmd?token=hpdj2620&c=sensors+watch+5')
        self.assertIn('400', head)
        self.assertIn('use the UART', res['msg'])
        # A shell command that polls the link itself (ping) works from the web too.
        _, res = self.http(vp, net, 'GET', '/api/cmd?token=hpdj2620&c=ping+192.168.50.1+2', budget=1_500_000_000)
        self.assertIn('ping: 2 of 2 answered', res['out'])
        self.assertIn('web> ping 192.168.50.1 2', vp.uart_text(0))

    def test_application_update_over_http(self):
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        image = Path(self.manifest_v2['targets']['app']['image']).read_bytes()
        head, res = self.http(vp, net, 'POST', '/api/update?token=wrong', image)
        self.assertIn('403', head)
        self.assertEqual(vp.flash_writes(), [])
        head, res = self.http(vp, net, 'POST', '/api/update?token=hpdj2620', image[:200] + bytes(800))
        self.assertIn('400', head)
        self.assertIn('invalid image', res['msg'])
        head, res = self.http(vp, net, 'POST', '/api/update?token=hpdj2620', image, budget=8_000_000_000)
        self.assertIn('200 OK', head, res)
        self.assertEqual(res, {'ok': True, 'slot': 'B', 'seq': 2, 'bytes': len(image), 'boots_next': True,
                               'reboot': False})
        text = vp.uart_text(0)
        self.assertIn(f'net: receiving a {len(image)} byte application image from 192.168.50.1', text)
        self.assertIn('net: slot B updated and verified; reboot to run it', text)
        out = vp.command('slots', PROMPT, max_instructions=LONG)
        self.assertRegex(out, r'B\s+0x0c0000\s+ok\s+1\.0\.1\s+2\s')

    def test_run_from_ram_over_http(self):
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        image = Path(self.manifest_hello['targets']['app']['image']).read_bytes()
        head, res = self.http(vp, net, 'POST', '/api/run?token=nope', image)
        self.assertIn('403', head)
        mark = len(vp.emu.uart)
        head, res = self.http(vp, net, 'POST', '/api/run?token=hpdj2620', image, budget=2_000_000_000)
        self.assertIn('200 OK', head, res)
        self.assertEqual(res['msg'], 'running from RAM')
        self.assertTrue(self.run_until(vp, lambda: b'hello> ' in vp.uart_bytes(mark), budget=3_000_000_000),
                        vp.uart_text(mark)[-600:])
        text = vp.uart_text(mark)
        self.assertIn('ramrun: starting v0.1.0 seq 1 from RAM', text)
        out = vp.command('help', b'hello> ')
        self.assertIn('hello', out)
        self.assertEqual(vp.flash_writes(), [])                     # nothing written to flash

    def test_telnet_shell(self):
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        net.raw_connect(23)
        self.assertTrue(self.run_until(vp, lambda: b'token: ' in net.raw_rx), bytes(net.raw_rx))
        self.assertIn(b'\xff\xfb\x01', net.raw_rx)                  # IAC WILL ECHO: the printer echoes
        net.raw_send(b'nope\r\n')
        self.assertTrue(self.run_until(vp, lambda: b'wrong token' in net.raw_rx))
        net.raw_send(b'hpdj2620\r\n')
        self.assertTrue(self.run_until(vp, lambda: b'app> ' in net.raw_rx.split(b'ok:')[-1]), bytes(net.raw_rx))
        net.raw_rx.clear()
        net.raw_send(b'version\r\n')
        self.assertTrue(self.run_until(vp, lambda: b'booted from slot' in net.raw_rx), bytes(net.raw_rx))
        self.assertRegex(bytes(net.raw_rx), rb'HPDJ app v1\.0\.0 seq 1 [^\r\n]*\r\n')   # CRLF line ends
        self.assertIn('net: telnet session from 192.168.50.1', vp.uart_text(0))
        net.raw_rx.clear()
        out = vp.command('echo from-uart', PROMPT)                       # UART output reaches telnet too
        self.assertTrue(self.run_until(vp, lambda: b'from-uart' in net.raw_rx))

    def test_virtual_front_panel(self):
        vp, net = self.boot()
        net.add_ap('Cafe-Open', None)
        vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        _, page = self.http(vp, net, 'GET', '/')
        self.assertIn('Front panel', page)
        self.run_until(vp, lambda: False, budget=200_000_000)            # let the panel menu draw
        _, p = self.http(vp, net, 'GET', '/api/panel')
        self.assertEqual(p['ui'], 1, p)                                   # menu running
        self.assertGreater(p['frames'], 0)
        mark = len(vp.emu.uart)
        _, p = self.http(vp, net, 'GET', '/api/panel?press=info&ms=400')
        self.assertTrue(p['ok'], p)
        self.assertTrue(self.run_until(vp, lambda: b'ui: info: address 192.168.50.100' in vp.uart_bytes(mark),
                                       budget=600_000_000), vp.uart_text(mark))
        _, p = self.http(vp, net, 'GET', '/api/panel?press=nosuch')
        self.assertFalse(p['ok'])
        self.assertEqual(p['msg'], 'unknown button')

    def test_motor_stall_blocks_pushing_on(self):
        vp, net = self.boot()
        out = vp.command('motor pos', PROMPT)
        self.assertIn('motor positions (encoder counts): b 0, c 0; a has no encoder', out)
        out = vp.command('motor home a', PROMPT)
        self.assertIn('motor a has no encoder', out)
        # The emulator's encoders never move, so a driven motor is "blocked" at once.
        net.add_ap('Cafe-Open', None)
        vp.command('wifi join Cafe-Open', PROMPT, max_instructions=LONG)
        mark = len(vp.emu.uart)
        _, res = self.http(vp, net, 'GET', '/api/motor?act=move&m=b&dir=fwd&pwm=300&ms=800')
        self.assertTrue(res['ok'])
        self.assertTrue(self.run_until(vp, lambda: b'stalled at position 0' in vp.uart_bytes(mark)))
        _, res = self.http(vp, net, 'GET', '/api/motor?act=move&m=b&dir=fwd&pwm=300&ms=800')
        self.assertFalse(res['ok'])
        self.assertEqual(res['msg'], 'stalled at the end stop: drive the other way')
        self.assertEqual(res['stall'], 1)
        _, res = self.http(vp, net, 'GET', '/api/motor?act=move&m=b&dir=rev&pwm=300&ms=100')
        self.assertTrue(res['ok'])
        self.http(vp, net, 'GET', '/api/motor?act=stop')


if __name__ == '__main__':
    unittest.main()
