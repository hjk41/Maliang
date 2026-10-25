import unittest

from pagewriter.protocol import (JobSession, JobState, LineParser, MachineProfile,
                                 SimulatedTransport, compile_machine_job, simulate_job)


class ParserTests(unittest.TestCase):
    def test_split_coalesced_status_and_pins(self):
        parser = LineParser()
        self.assertEqual(parser.feed(b"o"), [])
        events = parser.feed(b"k\r\n<Idle|MPos:0,0,15|WCO:0,0,15|Pn:XY>\nok\n")
        self.assertEqual([e.kind for e in events], ["ack", "status", "ack"])
        self.assertEqual(events[1].fields["MPos"], (0, 0, 15))
        self.assertEqual(events[1].fields["Pn"], "XY")

    def test_malformed_and_overflow(self):
        self.assertEqual(LineParser().feed(b"<Idle|MPos:nan,0,0>\n")[0].kind, "malformed")
        self.assertEqual(LineParser().feed(b"\xff\n")[0].kind, "malformed")
        self.assertEqual(LineParser(4).feed(b"12345")[0].kind, "malformed")


class SessionTests(unittest.TestCase):
    def test_ack_is_not_physical_completion(self):
        s = JobSession(["G21", "G1 X1"])
        self.assertEqual(s.send_next(), b"G21\n")
        self.assertIsNone(s.send_next())
        s.feed(b"ok\n")
        self.assertEqual(s.send_next(), b"G1 X1\n")
        s.feed(b"<Idle|MPos:0,0,0>\nok\n")
        self.assertEqual(s.state, JobState.WAITING_IDLE)
        s.feed(b"<Idle|MPos:0,0,0>\n")
        self.assertEqual(s.state, JobState.WAITING_IDLE)
        self.assertEqual(s.poll_status(), b"?")
        s.feed(b"<Run|MPos:0,0,0>\n")
        self.assertEqual(s.state, JobState.WAITING_IDLE)
        s.poll_status()
        s.feed(b"<Idle|MPos:1,0,0>\n")
        self.assertEqual(s.state, JobState.COMPLETE)
        self.assertEqual(s.sent_count, 2)

    def test_same_packet_duplicate_fails_without_sending_next(self):
        s = JobSession(["G21", "G90"])
        s.send_next()
        s.feed(b"ok\nok\n")
        self.assertEqual(s.state, JobState.FAILED)
        self.assertIsNone(s.send_next())
        self.assertEqual(s.transport.sent, [b"G21\n"])

    def test_partial_stale_idle_cannot_satisfy_final_query(self):
        s = JobSession(["G21"])
        s.send_next()
        s.feed(b"ok\n<Idle|MPos:")
        self.assertIsNone(s.poll_status())
        s.feed(b"0,0,0>\n")
        self.assertEqual(s.state, JobState.WAITING_IDLE)
        self.assertEqual(s.poll_status(), b"?")
        s.feed(b"<Idle|MPos:0,0,0>\n")
        self.assertEqual(s.state, JobState.COMPLETE)

    def test_running_poll_prohibited_no_old_query_can_remain(self):
        s = JobSession(["G1 X1 F60"])
        s.send_next()
        self.assertIsNone(s.poll_status())
        self.assertEqual(s.transport.sent, [b"G1 X1 F60\n"])
        # An unsolicited Idle before the final query cannot complete the job.
        s.feed(b"ok\n<Idle|MPos:0,0,15>\n")
        self.assertEqual(s.state, JobState.WAITING_IDLE)
        self.assertEqual(s.poll_status(), b"?")
        self.assertEqual(s.transport.sent, [b"G1 X1 F60\n", b"?"])
        s.feed(b"<Idle|MPos:1,0,15>\n")
        self.assertEqual(s.state, JobState.COMPLETE)

    def test_error_alarm_reset_unknown_disconnect_stop(self):
        for reply in [b"error:2\n", b"ALARM:1\n", b"Grbl 1.1h ['$' for help]\n", b"garbage\n", b"<Door:0|MPos:0,0,0>\n"]:
            with self.subTest(reply=reply):
                s = JobSession(["G21", "G90"])
                s.send_next()
                s.feed(reply)
                self.assertEqual(s.state, JobState.FAILED)
                self.assertIsNone(s.send_next())
                self.assertEqual(len(s.transport.sent), 1)
        s = JobSession(["G21"])
        s.disconnect()
        self.assertEqual(s.state, JobState.FAILED)

    def test_timeout_waiting_ack_and_idle(self):
        now = [1.0]
        for ack in [False, True]:
            with self.subTest(ack=ack):
                s = JobSession(["G21"], timeout=2, clock=lambda: now[0])
                s.send_next()
                if ack:
                    s.feed(b"ok\n")
                now[0] += 3
                self.assertTrue(s.check_timeout())
                self.assertEqual(s.state, JobState.FAILED)

    def test_exclusive_owner_and_cancel_no_motion(self):
        t = SimulatedTransport()
        s = JobSession(["G21"], t)
        with self.assertRaises(RuntimeError):
            JobSession(["G90"], t)
        s.send_next()
        s.cancel()
        self.assertEqual(t.sent, [b"G21\n"])
        self.assertIsNone(t.owner)

    def test_prohibited_commands_and_line_injection(self):
        for line in ["$H", "$X", "G10 P0 L20 X0", "G92 X0", "G010 X0", "G28.1", "G 10 X0", "G21\nG90", "!", "G1 X1\x18"]:
            with self.subTest(line=line), self.assertRaises(ValueError):
                JobSession([line])

    def test_demo(self):
        self.assertIn("仍未完成", simulate_job())
        self.assertIn("Idle", simulate_job())


class MachineProfileTests(unittest.TestCase):
    def good(self):
        return MachineProfile(page_to_machine=(0, 1, -1, 0, 100, 20), xy_verified=True,
            z_verified=True, clearance_verified=True, work_bounds=(0, 0, 200, 200),
            z_bounds=(-10, 20), z_up=-2, z_down=1, write_feed=100, z_feed=60,
            max_write_feed=200, max_z_feed=100, validation_note="synthetic test only")

    def test_default_never_compiles(self):
        with self.assertRaises(ValueError):
            compile_machine_job([[(1, 1), (2, 2)]], MachineProfile(), preflight_passed=True)

    def test_explicit_mapping_no_hidden_flip_or_home(self):
        p = self.good()
        self.assertEqual(p.map_point((10, 5)), (95, 30))
        lines = compile_machine_job([[(10, 5), (20, 5)]], p, preflight_passed=True)
        self.assertIn("G1 X95.0000 Y30.0000 F100.000", lines)
        self.assertIn("G1 X95.0000 Y40.0000 F100.000", lines)
        self.assertFalse(any("G10" in s or "$" in s for s in lines))
        self.assertEqual(lines[-1], "G1 Z-2.0000 F60.000")

    def test_preflight_range_speed_and_singular_map_reject(self):
        with self.assertRaises(ValueError):
            compile_machine_job([[(1, 1), (2, 2)]], self.good())
        with self.assertRaises(ValueError):
            compile_machine_job([[(1000, 1), (1001, 1)]], self.good(), preflight_passed=True)
        p = self.good()
        p.write_feed = 999
        with self.assertRaises(ValueError):
            p.validate()
        p = self.good()
        p.page_to_machine = (1, 1, 1, 1, 0, 0)
        with self.assertRaises(ValueError):
            p.validate()

    def test_independent_travel_write_and_z_feeds(self):
        p=self.good(); p.travel_feed=180; p.max_travel_feed=200
        lines=compile_machine_job([[(10,5),(20,5)],[(30,5),(40,5)]],p,preflight_passed=True)
        self.assertIn('G1 X95.0000 Y30.0000 F180.000',lines)
        self.assertIn('G1 X95.0000 Y50.0000 F180.000',lines)
        self.assertIn('G1 X95.0000 Y40.0000 F100.000',lines)
        self.assertIn('G1 Z1.0000 F60.000',lines)
        self.assertFalse(any(line.startswith('G0 ') for line in lines))

    def test_travel_feed_pair_and_limits(self):
        for feed,maximum in [(100,None),(None,100),(0,100),(-1,100),(101,100),
                             (float('nan'),100),(100,float('inf')),(100,float('nan'))]:
            p=self.good(); p.travel_feed=feed; p.max_travel_feed=maximum
            with self.subTest(feed=feed,maximum=maximum),self.assertRaises(ValueError): p.validate()
        p=self.good(); p.validate()
        lines=compile_machine_job([[(10,5),(20,5)]],p,preflight_passed=True)
        self.assertIn('G1 X95.0000 Y30.0000 F100.000',lines)


if __name__ == "__main__":
    unittest.main()

