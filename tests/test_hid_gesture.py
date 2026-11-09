import contextlib
import importlib
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import hid_gesture


class HidModuleImportTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(hid_gesture)

    def test_linux_prefers_hidraw_module_when_available(self):
        fake_hidraw = SimpleNamespace(device=object, enumerate=lambda *_args: [])
        fake_hid = SimpleNamespace(device=object, enumerate=lambda *_args: [])

        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(sys.modules, {"hidraw": fake_hidraw, "hid": fake_hid}),
        ):
            module = importlib.reload(hid_gesture)

        self.assertTrue(module.HIDAPI_OK)
        self.assertIs(module._hid, fake_hidraw)
        self.assertEqual(module._HID_MODULE_NAME, "hidraw")

    def test_linux_falls_back_to_hid_when_hidraw_module_is_absent(self):
        fake_hid = SimpleNamespace(device=object, enumerate=lambda *_args: [])

        with (
            patch.object(sys, "platform", "linux"),
            patch.dict(sys.modules, {"hidraw": None, "hid": fake_hid}),
        ):
            module = importlib.reload(hid_gesture)

        self.assertTrue(module.HIDAPI_OK)
        self.assertIs(module._hid, fake_hid)
        self.assertEqual(module._HID_MODULE_NAME, "hid")


class HidLinuxDiagnosticsTests(unittest.TestCase):
    def test_linux_logitech_hidraw_nodes_reads_sysfs_uevent(self):
        with tempfile.TemporaryDirectory() as tmp:
            node_dir = os.path.join(tmp, "hidraw3", "device")
            os.makedirs(node_dir)
            with open(os.path.join(node_dir, "uevent"), "w", encoding="utf-8") as fh:
                fh.write("HID_ID=0005:0000046D:0000B034\n")
                fh.write("HID_NAME=MX Master 3S\n")

            with patch.object(sys, "platform", "linux"):
                nodes = hid_gesture._linux_logitech_hidraw_nodes(base=tmp)

        self.assertEqual(nodes, ["hidraw3 PID=0xB034 product=MX Master 3S"])

    def test_summarize_hid_infos_includes_candidate_metadata(self):
        summary = hid_gesture._summarize_hid_infos([
            {
                "product_id": 0xB034,
                "usage_page": 0x0000,
                "usage": 0x0001,
                "transport": "Bluetooth Low Energy",
                "product_string": "MX Master 3S",
            }
        ])

        self.assertIn("PID=0xB034", summary)
        self.assertIn("UP=0x0000", summary)
        self.assertIn("product=MX Master 3S", summary)

    def test_format_linux_device_access_includes_path_permissions_and_access(self):
        with tempfile.NamedTemporaryFile() as fh:
            summary = hid_gesture._format_linux_device_access(fh.name.encode())

        self.assertIn("path=", summary)
        self.assertIn("mode=", summary)
        self.assertIn("owner=", summary)
        self.assertIn("group=", summary)
        self.assertIn("access=read:", summary)


class HidBackendPreferenceTests(unittest.TestCase):
    def test_default_backend_uses_auto_on_macos(self):
        self.assertEqual(hid_gesture._default_backend_preference("darwin"), "auto")

    def test_default_backend_uses_auto_elsewhere(self):
        self.assertEqual(hid_gesture._default_backend_preference("win32"), "auto")
        self.assertEqual(hid_gesture._default_backend_preference("linux"), "auto")


class GestureCandidateSelectionTests(unittest.TestCase):
    def test_choose_gesture_candidates_prefers_known_device_cids(self):
        listener = hid_gesture.HidGestureListener()
        device_spec = hid_gesture.resolve_device(product_id=0xB023)

        candidates = listener._choose_gesture_candidates(
            [
                {"cid": 0x00D7, "flags": 0x03B0, "mapping_flags": 0x0051},
                {"cid": 0x00C3, "flags": 0x0130, "mapping_flags": 0x0011},
            ],
            device_spec=device_spec,
        )

        self.assertEqual(candidates[:2], [0x00C3, 0x00D7])

    def test_choose_gesture_candidates_uses_capability_heuristic(self):
        listener = hid_gesture.HidGestureListener()

        candidates = listener._choose_gesture_candidates(
            [
                {"cid": 0x00A0, "flags": 0x0030, "mapping_flags": 0x0001},
                {"cid": 0x00F1, "flags": 0x01B0, "mapping_flags": 0x0011},
            ],
        )

        self.assertEqual(candidates[0], 0x00F1)

    def test_choose_gesture_candidates_falls_back_to_defaults(self):
        listener = hid_gesture.HidGestureListener()

        self.assertEqual(
            listener._choose_gesture_candidates([]),
            list(hid_gesture.DEFAULT_GESTURE_CIDS),
        )


class DeviceInfoDumpTests(unittest.TestCase):
    def test_dump_device_info_includes_runtime_capability_inventory(self):
        listener = hid_gesture.HidGestureListener()
        controls = [
            {
                "index": 0,
                "cid": 0x00D0,
                "task": 0x00AD,
                "flags": 0x0171,
                "mapped_to": 0x00D0,
                "mapping_flags": 0x0000,
            },
            {
                "index": 1,
                "cid": 0x005B,
                "task": 0x003F,
                "flags": 0x0171,
                "mapped_to": 0x005B,
                "mapping_flags": 0x0000,
            },
        ]
        listener._feat_idx = 0x0B
        listener._battery_idx = 0x08
        listener._battery_feature_id = hid_gesture.FEAT_BATTERY_STATUS
        listener._gesture_candidates = [0x00D0]
        listener._connected_device_info = hid_gesture.build_connected_device_info(
            product_id=0xB015,
            product_name="M720_Triathlon",
            reprog_controls=controls,
            gesture_cids=(0x00D0,),
            active_gesture_cid=0x00D0,
            gesture_rawxy_enabled=True,
            discovered_features=listener._discovered_feature_ids(),
        )
        listener._last_controls = controls

        dump = listener.dump_device_info()

        self.assertEqual(dump["device_key"], "m720_triathlon")
        self.assertIn("capability_inventory", dump)
        self.assertEqual(
            dump["capability_inventory"]["active_gesture_cid"],
            "0x00D0",
        )
        self.assertTrue(dump["capability_inventory"]["gesture_directions"])
        self.assertEqual(dump["capability_inventory"]["hscroll_cids"], ["0x005B"])
        self.assertTrue(dump["capability_inventory"]["battery"])


class _FakeHidDevice:
    def __init__(self):
        self.open_path = Mock()
        self.set_nonblocking = Mock()
        self.close = Mock()


class HidEnumerationFallbackTests(unittest.TestCase):
    @staticmethod
    def _printed_messages(print_mock):
        return [
            " ".join(str(arg) for arg in call.args)
            for call in print_mock.call_args_list
        ]

    def test_try_connect_accepts_known_device_without_usage_metadata(self):
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xB034,
            "usage_page": 0x0000,
            "usage": 0x0000,
            "transport": "Bluetooth Low Energy",
            "product_string": "MX Master 3S",
            "path": b"/dev/hidraw-test",
        }
        fake_dev = _FakeHidDevice()

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x10
            return None

        with (
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(
                    enumerate=lambda vid, pid: [info],
                    device=lambda: fake_dev,
                ),
                create=True,
            ),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch("builtins.print") as print_mock,
        ):
            self.assertTrue(listener._try_connect())

        messages = self._printed_messages(print_mock)
        self.assertTrue(
            any(
                "Accepting known Logitech device without vendor usage metadata"
                in message
                for message in messages
            )
        )
        self.assertEqual(listener.connected_device.display_name, "MX Master 3S")

    def test_vendor_hid_infos_logs_when_logitech_interfaces_are_filtered_out(self):
        info = {
            "product_id": 0x1234,
            "usage_page": 0x0000,
            "usage": 0x0000,
            "transport": "Bluetooth Low Energy",
            "product_string": "Unknown Logitech",
            "path": b"/dev/hidraw-test",
        }

        with (
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(enumerate=lambda vid, pid: [info]),
                create=True,
            ),
            patch("builtins.print") as print_mock,
        ):
            infos = hid_gesture.HidGestureListener._vendor_hid_infos()

        self.assertEqual(infos, [])
        messages = self._printed_messages(print_mock)
        self.assertTrue(
            any(
                "hidapi found Logitech interfaces, but none matched vendor "
                "usage metadata or known-device fallback"
                in message
                for message in messages
            )
        )


class HidDiscoveryDiagnosticsTests(unittest.TestCase):
    def _make_listener(self):
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xB023,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "transport": "Bluetooth Low Energy",
            "source": "hidapi-enumerate",
            "product_string": "MX Master 3",
            "path": b"/dev/hidraw-test",
        }
        return listener, info

    @staticmethod
    def _printed_messages(print_mock):
        return [
            " ".join(str(arg) for arg in call.args)
            for call in print_mock.call_args_list
        ]

    @staticmethod
    def _is_missing_reprog_diag(message):
        return (
            "Opened candidate but REPROG_V4 was not found "
            "on tested devIdx values"
        ) in message

    def test_try_connect_logs_missing_reprog_when_open_succeeds_for_all_dev_indices(self):
        listener, info = self._make_listener()
        fake_dev = _FakeHidDevice()

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", return_value=None),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print") as print_mock,
        ):
            self.assertFalse(listener._try_connect())

        messages = self._printed_messages(print_mock)
        self.assertTrue(
            any("Opened PID=0xB023 via hidapi" in message for message in messages)
        )
        self.assertTrue(
            any(self._is_missing_reprog_diag(message) for message in messages)
        )
        fake_dev.close.assert_called_once_with()

    def test_try_connect_logs_linux_hid_path_access_before_open(self):
        listener, info = self._make_listener()
        fake_dev = _FakeHidDevice()
        fake_dev.open_path.side_effect = OSError("open failed")

        with tempfile.NamedTemporaryFile() as fh:
            info = dict(info, path=fh.name.encode())
            with (
                patch.object(sys, "platform", "linux"),
                patch.object(listener, "_vendor_hid_infos", return_value=[info]),
                patch.object(hid_gesture, "HIDAPI_OK", True),
                patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
                patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
                patch.object(
                    hid_gesture,
                    "_hid",
                    SimpleNamespace(device=lambda: fake_dev),
                    create=True,
                ),
                patch("builtins.print") as print_mock,
            ):
                hid_gesture._LOG_ONCE_KEYS.clear()
                self.assertFalse(listener._try_connect())

        messages = self._printed_messages(print_mock)
        self.assertTrue(
            any("HID path access before open:" in message for message in messages)
        )
        self.assertTrue(any("access=read:" in message for message in messages))

    def test_try_connect_success_path_keeps_existing_reprog_discovery_diagnostics(self):
        listener, info = self._make_listener()
        fake_dev = _FakeHidDevice()

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x10
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print") as print_mock,
        ):
            self.assertTrue(listener._try_connect())

        messages = self._printed_messages(print_mock)
        self.assertTrue(
            any("Opened PID=0xB023 via hidapi" in message for message in messages)
        )
        self.assertTrue(
            any("Found REPROG_V4 @0x10" in message for message in messages)
        )
        self.assertFalse(
            any(self._is_missing_reprog_diag(message) for message in messages)
        )
        fake_dev.close.assert_not_called()

    def test_try_connect_rearms_extra_diverts_on_reconnect(self):
        listener = hid_gesture.HidGestureListener(
            extra_diverts={
                0x00C4: {"on_down": Mock(), "on_up": Mock()},
            }
        )
        info = {
            "product_id": 0xB023,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "transport": "Bluetooth Low Energy",
            "source": "hidapi-enumerate",
            "product_string": "MX Master 3",
            "path": b"/dev/hidraw-test",
        }
        fake_devs = [_FakeHidDevice(), _FakeHidDevice()]

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x10
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras") as divert_extras_mock,
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_devs.pop(0)),
                create=True,
            ),
        ):
            self.assertTrue(listener._try_connect())
            listener._dev = None
            self.assertTrue(listener._try_connect())

        self.assertEqual(divert_extras_mock.call_count, 2)
        self.assertIn(0x00C4, listener._extra_diverts)
        self.assertFalse(listener._extra_diverts[0x00C4]["held"])


class HidRequestTransportFailureTests(unittest.TestCase):
    def test_request_raises_ioerror_on_tx_failure_during_active_session(self):
        listener = hid_gesture.HidGestureListener()
        listener._connected = True

        with patch.object(listener, "_tx", side_effect=OSError("tx boom")):
            with self.assertRaises(IOError):
                listener._request(0x0E, 0, [])

    def test_request_raises_ioerror_on_rx_failure_during_active_session(self):
        listener = hid_gesture.HidGestureListener()
        listener._connected = True

        with (
            patch.object(listener, "_tx"),
            patch.object(listener, "_rx", side_effect=OSError("rx boom")),
        ):
            with self.assertRaises(IOError):
                listener._request(0x0E, 0, [])

    def test_request_returns_none_on_tx_failure_during_discovery(self):
        listener = hid_gesture.HidGestureListener()

        with patch.object(listener, "_tx", side_effect=OSError("tx boom")):
            self.assertIsNone(listener._request(0x0E, 0, []))

    def test_request_returns_none_on_rx_failure_during_discovery(self):
        listener = hid_gesture.HidGestureListener()

        with (
            patch.object(listener, "_tx"),
            patch.object(listener, "_rx", side_effect=OSError("rx boom")),
        ):
            self.assertIsNone(listener._request(0x0E, 0, []))

    def test_request_timeout_still_increments_timeout_counter(self):
        listener = hid_gesture.HidGestureListener()

        with (
            patch.object(listener, "_tx"),
            patch.object(listener, "_rx", return_value=None),
        ):
            self.assertIsNone(listener._request(0x0E, 0, [], timeout_ms=0))

        self.assertEqual(listener._consecutive_request_timeouts, 1)


class HidRequestReplyMatchingTests(unittest.TestCase):
    """Frames captured from an MX Master 4 on slot 2 of a Bolt receiver,
    with the software id rewritten to MY_SW. An IRoot lookup for REPROG_V4
    is `_request(0x00, 0, [0x1B, 0x04, 0x00])`."""

    # Reply from slot 2: REPROG_V4 is at feature index 0x0D. A sleeping
    # mouse sends it 550-840 ms after the request.
    SLOT_2_REPLY = [0x11, 0x02, 0x00, 0x0A, 0x0D] + [0x00] * 15
    # The receiver rejecting the same request for empty slot 1, ~8 ms after
    # it: HID++ 1.0 error 0x8F echoing sub-id 0x00 and address 0x0A.
    SLOT_1_REJECTION = [0x10, 0x01, 0x8F, 0x00, 0x0A, 0x09, 0x00]

    @staticmethod
    def _rx_returning(*frames):
        pending = list(frames)
        return lambda timeout_ms=2000: pending.pop(0) if pending else None

    def _request(self, listener, frames, timeout_ms=50):
        with (
            patch.object(listener, "_tx"),
            patch.object(listener, "_rx", side_effect=self._rx_returning(*frames)),
            patch.object(listener, "_on_report") as on_report,
            patch("builtins.print"),
        ):
            result = listener._request(0x00, 0, [0x1B, 0x04, 0x00],
                                       timeout_ms=timeout_ms)
        return result, on_report

    def test_accepts_a_reply_from_the_slot_it_asked(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev_idx = 0x02

        result, _ = self._request(listener, [self.SLOT_2_REPLY])

        self.assertEqual(result[0], 0x02)
        self.assertEqual(result[4][0], 0x0D)

    def test_ignores_a_late_reply_from_another_slot(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev_idx = 0x03

        result, on_report = self._request(listener, [self.SLOT_2_REPLY])

        self.assertIsNone(result)
        on_report.assert_called_once_with(self.SLOT_2_REPLY)

    def test_accepts_a_direct_device_answering_as_index_zero(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev_idx = hid_gesture.BT_DEV_IDX
        reply = [0x11, 0x00] + self.SLOT_2_REPLY[2:]

        result, _ = self._request(listener, [reply])

        self.assertIsNotNone(result)

    def test_a_receiver_rejection_fails_the_request_without_waiting(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev_idx = 0x01
        start = time.monotonic()

        result, _ = self._request(listener, [self.SLOT_1_REJECTION],
                                  timeout_ms=2000)

        self.assertIsNone(result)
        self.assertLess(time.monotonic() - start, 0.5)
        # Still counts towards the reconnect threshold: mid-session, a
        # rejection means the device has gone.
        self.assertEqual(listener._consecutive_request_timeouts, 1)

    def test_a_rejection_for_another_slot_does_not_end_the_request(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev_idx = 0x02

        result, _ = self._request(
            listener, [self.SLOT_1_REJECTION, self.SLOT_2_REPLY])

        self.assertIsNotNone(result)

    def test_a_rejection_of_a_different_request_does_not_end_this_one(self):
        listener = hid_gesture.HidGestureListener()
        listener._dev_idx = 0x01
        other_request = self.SLOT_1_REJECTION[:4] + [0x1A] + [0x09, 0x00]

        start = time.monotonic()
        result, _ = self._request(listener, [other_request], timeout_ms=200)

        self.assertIsNone(result)
        self.assertGreaterEqual(time.monotonic() - start, 0.2)


class HidLivenessPingTests(unittest.TestCase):
    """A resumed Windows box can leave our HID handle open against a device
    stack it has already replaced: reads never fail, they just never return
    anything again, so the listener sits "connected" against a dead pipe until
    the user restarts Moukey (issue #263). The ping is what turns that silence
    into a verdict."""

    def _connected_listener(self):
        listener = hid_gesture.HidGestureListener()
        listener._connected = True
        listener._dev = object()
        return listener

    def test_silence_is_unknown_until_a_connection_exists(self):
        listener = hid_gesture.HidGestureListener()

        self.assertIsNone(listener.seconds_since_last_report())

    def test_silence_is_measured_from_the_last_report(self):
        listener = self._connected_listener()
        listener._last_report_at = time.monotonic() - 42.0

        silence = listener.seconds_since_last_report()

        self.assertIsNotNone(silence)
        self.assertAlmostEqual(silence, 42.0, delta=1.0)

    def test_any_report_counts_as_proof_of_life(self):
        """Even a report we go on to ignore proves the pipe still carries
        traffic, so the stamp lands before parsing."""
        listener = self._connected_listener()
        listener._last_report_at = 0.0

        listener._on_report([0x11, 0x05, 0x00, 0x00])

        self.assertGreater(listener._last_report_at, 0.0)

    def test_request_liveness_ping_only_arms_a_flag(self):
        """Callers are other threads; the probe itself must run on the
        listener thread alongside every other HID++ exchange."""
        listener = self._connected_listener()

        listener.request_liveness_ping()

        self.assertTrue(listener._pending_liveness_ping)

    def test_answered_ping_clears_the_silence(self):
        listener = self._connected_listener()
        listener._last_report_at = 0.0
        listener._consecutive_request_timeouts = 2

        with patch.object(listener, "_request", return_value=("msg",)) as request:
            listener._run_liveness_ping()

        request.assert_called_once()
        self.assertGreater(listener._last_report_at, 0.0)
        self.assertEqual(listener._consecutive_request_timeouts, 0)

    def test_unanswered_ping_raises_to_force_a_reconnect(self):
        """IOError is the main loop's existing signal to tear the handle down
        and rebuild it, diverts and all."""
        listener = self._connected_listener()

        with patch.object(listener, "_request", return_value=None) as request:
            with self.assertRaises(IOError):
                listener._run_liveness_ping()

        # Retried once before giving up, so a single dropped reply does not
        # cost a reconnect.
        self.assertEqual(request.call_count, 2)

    def test_single_dropped_reply_is_survivable(self):
        listener = self._connected_listener()

        with patch.object(listener, "_request", side_effect=[None, ("msg",)]):
            listener._run_liveness_ping()

    def test_ping_targets_the_primary_session_not_the_cursor(self):
        """A sleeping keyboard under the cursor must not fail the probe."""
        listener = self._connected_listener()
        mouse = object()
        keyboard = object()
        listener._sessions = {
            0: {"_dev": mouse, "_dev_idx": 1},
            1: {"_dev": keyboard, "_dev_idx": 2},
        }
        listener._primary_session = 0
        seen = []

        with (
            patch.object(listener, "_load_cursor", side_effect=seen.append),
            patch.object(listener, "_request", return_value=("msg",)),
        ):
            listener._run_liveness_ping()

        self.assertEqual(seen, [listener._sessions[0]])

    def test_traffic_on_another_handle_does_not_prove_the_primary_alive(self):
        listener = self._connected_listener()
        mouse = object()
        keyboard = object()
        listener._sessions = {
            0: {"_dev": mouse, "_dev_idx": 1},
            1: {"_dev": keyboard, "_dev_idx": 2},
        }
        listener._primary_session = 0
        listener._last_report_at = 0.0

        listener._dev = keyboard
        listener._on_report([0x11, 0x05, 0x00, 0x00])
        self.assertEqual(listener._last_report_at, 0.0)

        listener._dev = mouse
        listener._on_report([0x11, 0x05, 0x00, 0x00])
        self.assertGreater(listener._last_report_at, 0.0)

    def test_connect_resets_liveness_state(self):
        listener = hid_gesture.HidGestureListener()
        listener._pending_liveness_ping = True
        listener._last_report_at = 0.0

        # Mirrors the main loop's post-connect bookkeeping: a fresh handle
        # starts its silence window now, with no probe left over from the
        # connection that just died.
        listener._connected = True
        listener._dev = object()
        listener._last_report_at = time.monotonic()
        listener._pending_liveness_ping = False

        self.assertLess(listener.seconds_since_last_report(), 1.0)
        self.assertFalse(listener._pending_liveness_ping)


class HidBoltReceiverTests(unittest.TestCase):
    """Tests for Logi Bolt receiver support."""

    def test_divert_failure_continues_to_next_receiver_slot(self):
        """When divert fails on one slot (e.g. keyboard), the loop
        continues and connects to the mouse on a later slot."""
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xC548,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "USB Receiver",
            "path": b"/dev/hidraw-test",
        }
        fake_dev = _FakeHidDevice()
        divert_call_count = [0]

        def fake_find_feature(feature_id, timeout_ms=2000):
            # Receiver: a keyboard on slot 1 (divert fails) and a mouse on slot 2
            # (divert succeeds); the multiplexer scan only binds where divert works.
            if (feature_id == hid_gesture.FEAT_REPROG_V4
                    and listener._dev_idx in (1, 2)):
                return 0x09
            return None

        def fake_divert():
            divert_call_count[0] += 1
            # First call fails (keyboard), second succeeds (mouse)
            return divert_call_count[0] >= 2

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", side_effect=fake_divert),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())
            self.assertEqual(divert_call_count[0], 2)

    def _probe_timeouts(self, info):
        """Run discovery against one candidate that has no REPROG_V4 and
        return the timeout each slot probe was given."""
        listener = hid_gesture.HidGestureListener()
        timeouts = []

        def fake_find_feature(feature_id, *, timeout_ms=None):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                timeouts.append(timeout_ms)
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=_FakeHidDevice),
                create=True,
            ),
            patch("core.hid_gesture._load_last_device_cache", return_value=None),
            patch("builtins.print"),
        ):
            listener._try_connect()
        return timeouts

    def test_receiver_slot_probes_wait_long_enough_for_a_sleeping_device(self):
        """A sleeping MX Master 4 answers its first request 550-840 ms late,
        and the receiver rejects an empty slot at once, so a receiver slot
        can afford to wait."""
        timeouts = self._probe_timeouts({
            "product_id": 0xC548, "usage_page": 0xFF00, "usage": 0x0002,
            "source": "hidapi-enumerate", "product_string": "USB Receiver",
            "path": b"/dev/hidraw-test",
        })

        self.assertEqual(len(timeouts), 6)
        self.assertTrue(all(t >= 1500 for t in timeouts), timeouts)

    def test_battery_broadcast_reports_owning_device_type(self):
        listener = hid_gesture.HidGestureListener()
        events = []
        listener._on_battery = lambda *args: events.append(args)
        listener._battery_idx = 0x08
        listener._device_type = "keyboard"

        listener._process_report(0x08, 0, [80, 0, 1], hid_gesture.MY_SW)
        self.assertEqual(events, [])  # our own polled getStatus response

        listener._process_report(0x08, 0, [80, 0, 1], 0)
        self.assertEqual(events, [(80, True, "keyboard")])

    def test_direct_device_probes_keep_the_short_timeout(self):
        """Nothing rejects a wrong index on a direct device -- an MX Brio
        never answers at all -- so each probe there costs its full wait."""
        timeouts = self._probe_timeouts({
            "product_id": 0x0944, "usage_page": 0xFF00, "usage": 0x0001,
            "source": "hidapi-enumerate", "product_string": "MX Brio",
            "path": b"/dev/hidraw-test",
        })

        self.assertEqual(len(timeouts), 7)
        self.assertTrue(all(t == 400 for t in timeouts), timeouts)

    def test_a_late_reply_does_not_settle_discovery_on_the_next_slot(self):
        """End to end through `_request`: the mouse on slot 2 answers only
        once slot 3 is being probed, and every other slot is empty."""
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xC548, "usage_page": 0xFF00, "usage": 0x0002,
            "source": "hidapi-enumerate", "product_string": "USB Receiver",
            "path": b"/dev/hidraw-test",
        }
        clock = [1000.0]

        class SlowMouseReceiver(_FakeHidDevice):
            def __init__(self):
                super().__init__()
                self.queue = []
                self.late_reply = None

            def write(self, buf):
                slot = buf[1]
                if self.late_reply is not None:
                    self.queue.append(self.late_reply)
                    self.late_reply = None
                if slot == 0x02:
                    self.late_reply = HidRequestReplyMatchingTests.SLOT_2_REPLY
                else:
                    self.queue.append(
                        [0x10, slot, 0x8F, buf[2], buf[3], 0x09, 0x00])

            def read(self, size, timeout_ms=0):
                if self.queue:
                    return self.queue.pop(0)
                clock[0] += (timeout_ms or 0) / 1000.0
                return None

        fake_time = SimpleNamespace(
            time=lambda: clock[0], monotonic=time.monotonic, sleep=time.sleep)

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_query_device_name", return_value=None),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "time", fake_time),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=SlowMouseReceiver),
                create=True,
            ),
            patch("core.hid_gesture._load_last_device_cache", return_value=None),
            patch("builtins.print"),
        ):
            connected = listener._try_connect()

        self.assertFalse(connected, f"settled on devIdx 0x{listener._dev_idx:02X}")

    def test_candidates_sorted_direct_devices_before_receivers(self):
        """Bluetooth devices should be tried before USB receivers."""
        listener = hid_gesture.HidGestureListener()
        infos = [
            {"product_string": "USB Receiver", "product_id": 0xC548,
             "usage_page": 0xFF00, "usage": 1, "source": "hidapi"},
            {"product_string": "MX Master 3S", "product_id": 0xB034,
             "usage_page": 0xFF43, "usage": 1, "source": "hidapi"},
            {"product_string": "USB Receiver", "product_id": 0xC548,
             "usage_page": 0xFF00, "usage": 2, "source": "hidapi"},
        ]

        with patch.object(listener, "_vendor_hid_infos", return_value=infos):
            # _try_connect sorts infos in place before iterating
            with (
                patch.object(listener, "_find_feature", return_value=None),
                patch("builtins.print"),
                patch(
                    "core.hid_gesture._load_last_device_cache",
                    return_value=None,
                ),
            ):
                listener._try_connect()

        # After sorting, direct device should be first
        self.assertEqual(infos[0]["product_string"], "MX Master 3S")

    def test_transport_label_bluetooth_for_direct_connection(self):
        """devIdx 0xFF should produce 'Bluetooth' transport."""
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xB034,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "MX Master 3S",
            "path": b"/dev/hidraw-test",
        }
        fake_dev = _FakeHidDevice()

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x09
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        # devIdx 0xFF (first tried) = Bluetooth
        self.assertEqual(listener.connected_device.transport, "Bluetooth")

    def test_try_connect_applies_runtime_supported_buttons(self):
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xB034,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "MX Master 3S",
            "path": b"/dev/hidraw-test",
        }
        controls = [
            {"cid": 0x0052, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x0053, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x0056, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x00C3, "flags": 0x0130, "mapping_flags": 0x0011},
        ]
        fake_dev = _FakeHidDevice()

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x09
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=controls),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        self.assertIn("gesture", listener.connected_device.supported_buttons)
        self.assertNotIn("gesture_up", listener.connected_device.supported_buttons)
        self.assertNotIn("mode_shift", listener.connected_device.supported_buttons)

    def test_try_connect_marks_thumb_button_cid_button_only_for_mx_master_4(self):
        """MX Master 4 must mark its thumb_button CID (the small HID++
        button at 0x00C3) as button-only so the rawXY-enabled divert is
        skipped if it ever ends up as the active gesture CID. Without
        that the firmware suppresses OS mouse motion while the user
        holds the button, freezing the cursor for nothing -- its rawXY
        data is irrelevant when gestures are routed through the haptic
        panel."""
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xB042,  # MX Master 4
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "MX Master 4",
            "path": b"/dev/hidraw-test",
        }
        fake_dev = _FakeHidDevice()

        def fake_find_feature(feature_id, *, timeout_ms=None):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x09
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(listener, "_install_thumb_button_extra"),
            patch.object(listener, "_query_device_name", return_value=None),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        self.assertIn(
            0x00C3, listener._button_only_cids,
            "MX Master 4's small HID++ button (thumb_button_cid) must be "
            "added to _button_only_cids so the rawXY divert is skipped -- "
            "rawXY would freeze the cursor while held.",
        )

    def test_try_connect_leaves_button_only_cids_empty_for_mx_master_3s(self):
        """Older MX Masters have no thumb_button_cid, so no CID needs to be
        forced into button-only mode. The default rawXY-enabled divert on
        the gesture CID is what drives directional swipes on those mice."""
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xB034,  # MX Master 3S
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "MX Master 3S",
            "path": b"/dev/hidraw-test",
        }
        fake_dev = _FakeHidDevice()

        def fake_find_feature(feature_id, *, timeout_ms=None):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x09
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(listener, "_install_thumb_button_extra"),
            patch.object(listener, "_query_device_name", return_value=None),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        self.assertEqual(listener._button_only_cids, set())

    def test_divert_skips_rawxy_attempt_for_cids_in_button_only_set(self):
        """`_divert` must request 0x03 (button-only) directly for any CID
        flagged in `_button_only_cids`, rather than first trying 0x33
        (rawXY-enabled) and falling back. This is the per-CID equivalent
        of the old global button-only flag."""
        listener = hid_gesture.HidGestureListener()
        listener._feat_idx = 0x09
        listener._gesture_candidates = [0x00C3]
        listener._button_only_cids = {0x00C3}

        recorded = []

        def fake_set_cid_reporting(cid, flags):
            recorded.append((cid, flags))
            return True

        with (
            patch.object(listener, "_set_cid_reporting", side_effect=fake_set_cid_reporting),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._divert())

        self.assertEqual(recorded, [(0x00C3, 0x03)])
        self.assertFalse(listener._rawxy_enabled)

    def test_divert_tries_rawxy_first_for_cids_not_in_button_only_set(self):
        """Default behavior -- including for the new haptic CID 0x01A0 on
        MX Master 4 -- is to request rawXY-enabled divert (0x33) so the
        firmware delivers swipe motion over the vendor channel and pins
        the cursor on its own."""
        listener = hid_gesture.HidGestureListener()
        listener._feat_idx = 0x09
        listener._gesture_candidates = [0x01A0]
        listener._button_only_cids = {0x00C3}  # only small button is button-only

        recorded = []

        def fake_set_cid_reporting(cid, flags):
            recorded.append((cid, flags))
            return True

        with (
            patch.object(listener, "_set_cid_reporting", side_effect=fake_set_cid_reporting),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._divert())

        self.assertEqual(recorded, [(0x01A0, 0x33)])
        self.assertTrue(listener._rawxy_enabled)

    def test_install_thumb_button_extra_adds_cid_when_distinct_from_gesture(self):
        """Happy path: 0x01A0 is the active gesture CID on MX Master 4,
        so 0x00C3 (thumb_button_cid) is added as a button-only extra with
        thumb_button callbacks wired in. ``thumb_button_via_hid`` stays
        False until ``_divert_extras`` acknowledges the setCidReporting."""
        on_down = Mock()
        on_up = Mock()
        listener = hid_gesture.HidGestureListener(
            on_thumb_button_down=on_down,
            on_thumb_button_up=on_up,
        )
        listener._gesture_cid = 0x01A0
        device_spec = SimpleNamespace(thumb_button_cid=0x00C3)
        controls = [
            {"cid": 0x00C3, "flags": 0x0030, "mapping_flags": 0x0001},
        ]

        with patch("builtins.print"):
            listener._install_thumb_button_extra(device_spec, controls)

        self.assertEqual(listener._thumb_button_cid, 0x00C3)
        self.assertIn(0x00C3, listener._extra_diverts)
        # No ack yet -- divert_extras has not run.
        self.assertFalse(listener.thumb_button_via_hid)

        listener._extra_divert_acks.add(0x00C3)
        self.assertTrue(listener.thumb_button_via_hid)

        # Trigger the wired callbacks via the extras dispatch path.
        listener._extra_diverts[0x00C3]["on_down"]()
        on_down.assert_called_once()
        listener._extra_diverts[0x00C3]["on_up"]()
        on_up.assert_called_once()

    def test_install_thumb_button_extra_skipped_when_cid_absent_from_reprog(self):
        """Catalog declares a thumb_button CID, but the firmware does not
        advertise it on this connection. The helper must refuse to queue the
        divert -- queueing would hammer setCidReporting with a CID the
        firmware never exposed.
        """
        listener = hid_gesture.HidGestureListener(
            on_thumb_button_down=Mock(),
            on_thumb_button_up=Mock(),
        )
        listener._gesture_cid = 0x01A0
        device_spec = SimpleNamespace(thumb_button_cid=0x00C3)
        controls = [
            {"cid": 0x0052, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x01A0, "flags": 0x0130, "mapping_flags": 0x0011},
        ]

        with patch("builtins.print"):
            listener._install_thumb_button_extra(device_spec, controls)

        self.assertIsNone(listener._thumb_button_cid)
        self.assertNotIn(0x00C3, listener._extra_diverts)
        self.assertFalse(listener.thumb_button_via_hid)

    def test_divert_extras_clears_acks_and_drops_failed_cids(self):
        """When setCidReporting fails for a CID, the listener must drop it
        from ``_extra_diverts`` so the OS-fallback path stays in charge of
        that button. ``thumb_button_via_hid`` then resolves to False.
        """
        listener = hid_gesture.HidGestureListener()
        listener._feat_idx = 0x09
        listener._thumb_button_cid = 0x00C3
        listener._extra_diverts = {
            0x00C4: {"on_down": Mock(), "on_up": Mock(), "held": False},
            0x00C3: {"on_down": Mock(), "on_up": Mock(), "held": False},
        }
        responses = {0x00C4: True, 0x00C3: None}

        def fake_set_cid_reporting(cid, flags):
            return responses.get(cid)

        with (
            patch.object(listener, "_set_cid_reporting", side_effect=fake_set_cid_reporting),
            patch("builtins.print"),
        ):
            listener._divert_extras()

        self.assertEqual(listener._extra_divert_acks, {0x00C4})
        self.assertIn(0x00C4, listener._extra_diverts)
        self.assertNotIn(0x00C3, listener._extra_diverts)
        self.assertIsNone(listener._thumb_button_cid)
        self.assertFalse(listener.thumb_button_via_hid)

    def test_install_thumb_button_extra_skipped_when_same_as_gesture_cid(self):
        """Fallback path: 0x01A0 divert was rejected, so 0x00C3 became
        the gesture CID. The thumb_button extra must NOT be added -- that
        would re-divert the same CID and stomp on the gesture flags."""
        listener = hid_gesture.HidGestureListener(
            on_thumb_button_down=Mock(),
            on_thumb_button_up=Mock(),
        )
        listener._gesture_cid = 0x00C3
        device_spec = SimpleNamespace(thumb_button_cid=0x00C3)
        controls = [
            {"cid": 0x00C3, "flags": 0x0030, "mapping_flags": 0x0001},
        ]

        with patch("builtins.print"):
            listener._install_thumb_button_extra(device_spec, controls)

        self.assertIsNone(listener._thumb_button_cid)
        self.assertNotIn(0x00C3, listener._extra_diverts)
        self.assertFalse(listener.thumb_button_via_hid)

    def test_install_thumb_button_extra_no_op_when_cid_unset(self):
        listener = hid_gesture.HidGestureListener()
        listener._gesture_cid = 0x00C3
        device_spec = SimpleNamespace(thumb_button_cid=None)

        with patch("builtins.print"):
            listener._install_thumb_button_extra(device_spec, [])

        self.assertIsNone(listener._thumb_button_cid)
        self.assertEqual(listener._extra_diverts, {})
        self.assertFalse(listener.thumb_button_via_hid)

    def test_mx_master_4_full_connect_wires_haptic_gesture_and_thumb_button_extra(self):
        """End-to-end happy path: when MX Master 4 connects and the
        haptic CID 0x01A0 is divertable with rawXY, the listener picks
        it as the active gesture CID and ALSO installs 0x00C3 as the
        thumb_button extra. The resulting ConnectedDeviceInfo carries
        both flags so platform mouse hooks can drop their OS-level
        fallback paths and let HID++ own both buttons end-to-end."""
        on_action_down = Mock()
        on_action_up = Mock()
        listener = hid_gesture.HidGestureListener(
            on_thumb_button_down=on_action_down,
            on_thumb_button_up=on_action_up,
        )
        info = {
            "product_id": 0xB042,  # MX Master 4
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "MX Master 4",
            "path": b"/dev/hidraw-test",
        }
        # Simulate the device exposing both the haptic CID (0x01A0 with
        # rawXY) and the small button (0x00C3, also rawXY-capable) plus
        # back/forward.
        controls = [
            {"cid": 0x0053, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x0056, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x00C3, "flags": 0x0130, "mapping_flags": 0x0011},
            {"cid": 0x01A0, "flags": 0x0130, "mapping_flags": 0x0011},
        ]
        fake_dev = _FakeHidDevice()
        cid_calls: list[tuple[int, int]] = []

        def fake_set_cid_reporting(cid, flags):
            cid_calls.append((cid, flags))
            return True  # firmware accepts every divert

        def fake_find_feature(feature_id, *, timeout_ms=None):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x09
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=controls),
            patch.object(listener, "_set_cid_reporting", side_effect=fake_set_cid_reporting),
            patch.object(listener, "_query_device_name", return_value="MX Master 4"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        # Active gesture CID should be the sense panel.
        self.assertEqual(listener._gesture_cid, 0x01A0)
        # And it should have been diverted with rawXY (0x33), not 0x03.
        self.assertIn(
            (0x01A0, 0x33), cid_calls,
            "haptic CID must be diverted with rawXY so swipe data flows "
            f"over HID++; setCidReporting calls were {cid_calls}",
        )
        # Action ring extra installed for 0x00C3, button-only.
        self.assertEqual(listener._thumb_button_cid, 0x00C3)
        self.assertIn(0x00C3, listener._extra_diverts)
        self.assertIn(
            (0x00C3, 0x03), cid_calls,
            "thumb_button CID must be diverted button-only (no rawXY) so "
            "the firmware doesn't suppress cursor motion while the "
            f"small button is held; setCidReporting calls were {cid_calls}",
        )
        # ConnectedDeviceInfo reflects the wiring.
        self.assertEqual(
            listener.connected_device.active_gesture_cid, 0x01A0
        )
        self.assertTrue(listener.connected_device.thumb_button_via_hid)

    def test_install_thumb_button_extra_clears_stale_entry_on_reconnect(self):
        """Reconnect to a different device whose spec has no thumb_button
        CID -- the previously-installed entry must be removed so it doesn't
        leak across devices."""
        listener = hid_gesture.HidGestureListener(
            on_thumb_button_down=Mock(),
            on_thumb_button_up=Mock(),
        )
        listener._gesture_cid = 0x01A0
        first_controls = [
            {"cid": 0x00C3, "flags": 0x0030, "mapping_flags": 0x0001},
        ]
        with patch("builtins.print"):
            listener._install_thumb_button_extra(
                SimpleNamespace(thumb_button_cid=0x00C3),
                first_controls,
            )
        self.assertIn(0x00C3, listener._extra_diverts)

        listener._gesture_cid = 0x00C3  # different device
        with patch("builtins.print"):
            listener._install_thumb_button_extra(
                SimpleNamespace(thumb_button_cid=None),
                [],
            )

        self.assertNotIn(0x00C3, listener._extra_diverts)
        self.assertIsNone(listener._thumb_button_cid)

    def test_try_connect_preserves_directional_gestures_after_rawxy_divert(self):
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xB034,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "MX Master 3S",
            "path": b"/dev/hidraw-test",
        }
        controls = [
            {"cid": 0x0052, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x0053, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x0056, "flags": 0x0030, "mapping_flags": 0x0001},
            {"cid": 0x00C3, "flags": 0x0130, "mapping_flags": 0x0011},
            {"cid": 0x00C4, "flags": 0x0130, "mapping_flags": 0x0001},
        ]
        fake_dev = _FakeHidDevice()

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x09
            return None

        def fake_divert():
            listener._gesture_cid = 0x00C3
            listener._rawxy_enabled = True
            return True

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=controls),
            patch.object(listener, "_divert", side_effect=fake_divert),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        self.assertIn("gesture", listener.connected_device.supported_buttons)
        self.assertIn("gesture_up", listener.connected_device.supported_buttons)
        self.assertIn("mode_shift", listener.connected_device.supported_buttons)

    def test_transport_label_logi_bolt_for_bolt_receiver(self):
        """devIdx 1-6 with Bolt PID 0xC548 should produce 'Logi Bolt'."""
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xC548,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "USB Receiver",
            "path": b"/dev/hidraw-test",
        }
        fake_dev = _FakeHidDevice()
        call_count = [0]

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id != hid_gesture.FEAT_REPROG_V4:
                return None
            call_count[0] += 1
            return 0x09 if call_count[0] >= 2 else None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
            patch(
                "core.hid_gesture._load_last_device_cache",
                return_value=None,
            ),
        ):
            self.assertTrue(listener._try_connect())

        self.assertEqual(listener.connected_device.transport, "Logi Bolt")

    def test_transport_label_usb_receiver_for_non_bolt(self):
        """devIdx 1-6 with non-Bolt PID (e.g. Unifying 0xC52B) should produce
        'USB Receiver', not 'Logi Bolt'."""
        listener = hid_gesture.HidGestureListener()
        info = {
            "product_id": 0xC52B,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "source": "hidapi-enumerate",
            "product_string": "USB Receiver",
            "path": b"/dev/hidraw-test",
        }
        fake_dev = _FakeHidDevice()
        call_count = [0]

        def fake_find_feature(feature_id, timeout_ms=2000):
            if feature_id != hid_gesture.FEAT_REPROG_V4:
                return None
            call_count[0] += 1
            return 0x09 if call_count[0] >= 2 else None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=[info]),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        self.assertEqual(listener.connected_device.transport, "USB Receiver")


class HidMultipleBluetoothTests(unittest.TestCase):
    """Two Bluetooth devices are separate HID interfaces (separate handles) that
    both report on device-index 0xFF; the multiplexer must bind both, not just
    the first interface it succeeds on."""

    def test_binds_both_direct_bluetooth_devices(self):
        listener = hid_gesture.HidGestureListener()
        infos = [
            {"product_id": 0xB023, "usage_page": 0xFF00, "usage": 0x0001,
             "source": "hidapi-enumerate", "product_string": "MX Master 3",
             "serial_number": "aa:bb:cc:dd:ee:01", "path": b"/dev/hidraw-a"},
            {"product_id": 0xB035, "usage_page": 0xFF00, "usage": 0x0001,
             "source": "hidapi-enumerate", "product_string": "MX Keys",
             "serial_number": "aa:bb:cc:dd:ee:02", "path": b"/dev/hidraw-b"},
        ]

        def fake_find_feature(feature_id, timeout_ms=2000):
            # Both devices answer REPROG_V4 on the direct index 0xFF only.
            if (feature_id == hid_gesture.FEAT_REPROG_V4
                    and listener._dev_idx == hid_gesture.BT_DEV_IDX):
                return 0x09
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=infos),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                # A fresh handle per open(): distinct Bluetooth interfaces.
                SimpleNamespace(device=lambda: _FakeHidDevice()),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        # Both devices bound as separate sessions on two distinct handles.
        self.assertEqual(len(listener._sessions), 2)
        self.assertEqual(len(listener._distinct_session_handles()), 2)
        names = sorted(d.product_name for d in listener.connected_devices)
        self.assertEqual(names, ["MX Keys", "MX Master 3"])

    def test_skips_second_collection_of_same_bluetooth_device(self):
        """A single BLE device exposes several 0xFF00 collections (same serial);
        it must be bound once, not once per collection."""
        listener = hid_gesture.HidGestureListener()
        infos = [
            {"product_id": 0xB023, "usage_page": 0xFF00, "usage": 0x0002,
             "source": "hidapi-enumerate", "product_string": "MX Master 3",
             "serial_number": "aa:bb:cc:dd:ee:01", "path": b"/dev/hidraw-a&col01"},
            {"product_id": 0xB023, "usage_page": 0xFF00, "usage": 0x0001,
             "source": "hidapi-enumerate", "product_string": "MX Master 3",
             "serial_number": "aa:bb:cc:dd:ee:01", "path": b"/dev/hidraw-a&col02"},
        ]

        def fake_find_feature(feature_id, timeout_ms=2000):
            if (feature_id == hid_gesture.FEAT_REPROG_V4
                    and listener._dev_idx == hid_gesture.BT_DEV_IDX):
                return 0x09
            return None

        with (
            patch.object(listener, "_vendor_hid_infos", return_value=infos),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: _FakeHidDevice()),
                create=True,
            ),
            patch("builtins.print"),
        ):
            self.assertTrue(listener._try_connect())

        self.assertEqual(len(listener._sessions), 1)


class HidReconnectInvariantTests(unittest.TestCase):
    def test_force_release_stale_holds_clears_gesture_and_extra_buttons(self):
        gesture_up = Mock()
        extra_up = Mock()
        listener = hid_gesture.HidGestureListener(
            on_up=gesture_up,
            extra_diverts={0x00C4: {"on_up": extra_up}},
        )
        listener._held = True
        listener._extra_diverts[0x00C4]["held"] = True

        listener._force_release_stale_holds()

        self.assertFalse(listener._held)
        self.assertFalse(listener._extra_diverts[0x00C4]["held"])
        gesture_up.assert_called_once_with()
        extra_up.assert_called_once_with()


class MultiDeviceFeatureFlagTests(unittest.TestCase):
    """backlight/crown support must consider every bound device, not just the
    live cursor (which flips between devices as reports are processed)."""

    def test_backlight_supported_true_when_any_session_has_it(self):
        listener = hid_gesture.HidGestureListener()
        listener._sessions = {
            0x02: {"_backlight_idx": 0x0A, "_crown_idx": 0x12},  # keyboard
            0x03: {"_backlight_idx": None, "_crown_idx": None},  # mouse
        }
        # Cursor currently points at the mouse (no backlight)…
        listener._backlight_idx = None
        listener._crown_idx = None
        # …but support must still be reported from the keyboard session.
        self.assertTrue(listener.backlight_supported)
        self.assertTrue(listener.crown_present)

    def test_backlight_supported_false_when_no_session_has_it(self):
        listener = hid_gesture.HidGestureListener()
        listener._sessions = {0x03: {"_backlight_idx": None, "_crown_idx": None}}
        listener._backlight_idx = None
        listener._crown_idx = None
        self.assertFalse(listener.backlight_supported)
        self.assertFalse(listener.crown_present)

    def test_single_device_falls_back_to_cursor(self):
        listener = hid_gesture.HidGestureListener()
        listener._sessions = {}
        listener._backlight_idx = 0x0A
        self.assertTrue(listener.backlight_supported)

class MxMaster4ConstantTests(unittest.TestCase):
    def test_sense_panel_cid_named(self):
        self.assertIn(0x01A0, hid_gesture.KNOWN_CID_NAMES)
        self.assertEqual(hid_gesture.KNOWN_CID_NAMES[0x01A0], "Sense Panel")

    def test_haptic_feature_constant(self):
        self.assertEqual(hid_gesture.FEAT_HAPTIC, 0x19B0)

    def test_force_sensing_constant(self):
        self.assertEqual(hid_gesture.FEAT_FORCE_SENSING, 0x19C0)


class HidReconnectStormTests(unittest.TestCase):
    """Regression coverage for issue #238: the reconnect/probe throttle.

    These exercise the platform-independent connect logic (via the hidapi
    fake). The IOKit manager/port leak fix itself is macOS-only and must be
    validated on-device with `leaks`/`heap`; see the issue for that harness."""

    @staticmethod
    def _printed_messages(print_mock):
        return [
            " ".join(str(arg) for arg in call.args)
            for call in print_mock.call_args_list
        ]

    @staticmethod
    def _info(pid, product, path):
        return {
            "product_id": pid,
            "usage_page": 0xFF00,
            "usage": 0x0001,
            "transport": "Bluetooth Low Energy",
            "source": "hidapi-enumerate",
            "product_string": product,
            "path": path,
        }

    @contextlib.contextmanager
    def _connecting(self, listener, infos, fake_dev, *, reprog_found=True):
        def fake_find_feature(feature_id, *, timeout_ms=None):
            if reprog_found and feature_id == hid_gesture.FEAT_REPROG_V4:
                return 0x10
            return None

        cms = (
            patch.object(hid_gesture, "HIDAPI_OK", True),
            patch.object(hid_gesture, "_BACKEND_PREFERENCE", "hidapi"),
            patch.object(hid_gesture, "_HID_API_STYLE", "hidapi"),
            patch.object(
                hid_gesture,
                "_hid",
                SimpleNamespace(device=lambda: fake_dev),
                create=True,
            ),
            patch.object(hid_gesture, "_load_last_device_cache", return_value=None),
            patch.object(hid_gesture, "_save_last_device_cache"),
            patch.object(listener, "_vendor_hid_infos", return_value=infos),
            patch.object(listener, "_find_feature", side_effect=fake_find_feature),
            patch.object(listener, "_discover_reprog_controls", return_value=[]),
            patch.object(listener, "_divert", return_value=True),
            patch.object(listener, "_divert_extras"),
        )
        with contextlib.ExitStack() as stack:
            for cm in cms:
                stack.enter_context(cm)
            yield

    def test_candidate_cooldown_key_is_hashable_and_distinct(self):
        a = self._info(0xB034, "Iface A", b"/dev/hidraw-a")
        b = self._info(0xB023, "Iface B", b"/dev/hidraw-b")
        key_a = hid_gesture._candidate_cooldown_key(a)
        self.assertEqual(key_a, hid_gesture._candidate_cooldown_key(a))
        self.assertNotEqual(key_a, hid_gesture._candidate_cooldown_key(b))
        self.assertIsInstance(hash(key_a), int)

    def test_missing_reprog_records_cooldown(self):
        listener = hid_gesture.HidGestureListener()
        info = self._info(0xB034, "MX Master 3S", b"/dev/hidraw-x")
        fake_dev = _FakeHidDevice()
        with self._connecting(listener, [info], fake_dev, reprog_found=False):
            self.assertFalse(listener._try_connect())
        self.assertIn(
            hid_gesture._candidate_cooldown_key(info),
            listener._reprog_absent_until,
        )

    def test_cooled_interface_is_skipped_when_another_candidate_exists(self):
        listener = hid_gesture.HidGestureListener()
        # Name sorts the cooled interface first so the skip branch runs before
        # the good interface connects.
        cooled = self._info(0xB025, "AAA Cooled Iface", b"/dev/hidraw-cooled")
        good = self._info(0xB034, "MX Master 3S", b"/dev/hidraw-good")
        listener._reprog_absent_until[
            hid_gesture._candidate_cooldown_key(cooled)
        ] = time.monotonic() + 999
        fake_dev = _FakeHidDevice()
        with self._connecting(listener, [cooled, good], fake_dev):
            with patch("builtins.print") as print_mock:
                self.assertTrue(listener._try_connect())
        # Only the non-cooled interface was opened.
        fake_dev.open_path.assert_called_once_with(b"/dev/hidraw-good")
        self.assertTrue(
            any(
                "Skipping recently-incompatible interface" in message
                for message in self._printed_messages(print_mock)
            )
        )

    def test_sole_cooled_interface_is_still_probed(self):
        listener = hid_gesture.HidGestureListener()
        only = self._info(0xB034, "MX Master 3S", b"/dev/hidraw-only")
        listener._reprog_absent_until[
            hid_gesture._candidate_cooldown_key(only)
        ] = time.monotonic() + 999
        fake_dev = _FakeHidDevice()
        with self._connecting(listener, [only], fake_dev):
            # A lone candidate is never skipped, even while cooling down, so a
            # just-woken device reconnects without waiting out the cooldown.
            self.assertTrue(listener._try_connect())
        fake_dev.open_path.assert_called_once_with(b"/dev/hidraw-only")

    def test_interruptible_sleep_returns_immediately_when_stopped(self):
        listener = hid_gesture.HidGestureListener()
        listener._running = False
        start = time.monotonic()
        listener._interruptible_sleep(5)
        self.assertLess(time.monotonic() - start, 0.5)

    def test_device_change_wakes_reconnect_backoff(self):
        listener = hid_gesture.HidGestureListener()
        listener._running = True
        listener.request_rescan()

        start = time.monotonic()
        listener._interruptible_sleep(30)

        self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse(listener._rescan_event.is_set())


class DivertHealthCheckTests(unittest.TestCase):
    def _make_listener(self, reporting_flags=0x33):
        listener = hid_gesture.HidGestureListener()
        listener._feat_idx = 0x0D
        listener._gesture_cid = 0x01A0
        listener._get_cid_reporting = Mock(return_value=(
            0x02, 0x0D, 2, hid_gesture.MY_SW,
            [0x01, 0xA0, reporting_flags, 0x00, 0x00],
        ))
        return listener

    def test_lost_divert_raises_to_trigger_reconnect(self):
        listener = self._make_listener(reporting_flags=0x00)
        listener._divert_check_due = True
        with self.assertRaises(IOError):
            listener._maybe_verify_divert(now=100.0)
        listener._get_cid_reporting.assert_called_once_with(0x01A0)

    def test_intact_divert_is_left_alone(self):
        listener = self._make_listener(reporting_flags=0x33)
        listener._divert_check_due = True
        listener._maybe_verify_divert(now=100.0)
        self.assertFalse(listener._divert_check_due)
        self.assertEqual(listener._last_divert_check, 100.0)

    def test_no_answer_does_not_reconnect(self):
        listener = self._make_listener()
        listener._get_cid_reporting = Mock(return_value=None)
        listener._divert_check_due = True
        listener._maybe_verify_divert(now=100.0)

    def test_periodic_check_waits_for_interval(self):
        listener = self._make_listener()
        listener._last_divert_check = 100.0
        with patch.object(hid_gesture, "_user_idle_seconds", return_value=0.0):
            listener._maybe_verify_divert(now=110.0)
            listener._get_cid_reporting.assert_not_called()
            listener._maybe_verify_divert(now=131.0)
        listener._get_cid_reporting.assert_called_once()

    def test_periodic_check_skipped_while_user_idle(self):
        listener = self._make_listener()
        listener._last_divert_check = 0.0
        with patch.object(hid_gesture, "_user_idle_seconds", return_value=600.0):
            listener._maybe_verify_divert(now=1000.0)
        listener._get_cid_reporting.assert_not_called()

    def test_due_check_ignores_idle_and_skips_while_held(self):
        listener = self._make_listener()
        listener._divert_check_due = True
        listener._held = True
        with patch.object(hid_gesture, "_user_idle_seconds", return_value=600.0):
            listener._maybe_verify_divert(now=1000.0)
            listener._get_cid_reporting.assert_not_called()
            listener._held = False
            listener._maybe_verify_divert(now=1000.0)
        listener._get_cid_reporting.assert_called_once()

    def test_check_targets_the_primary_device_not_the_cursor(self):
        """With a keyboard under the cursor the check must still look at the
        mouse's gesture divert, and a keyboard-only cursor must not raise."""
        listener = self._make_listener(reporting_flags=0x00)
        mouse = listener._snapshot_cursor()
        mouse["_session_key"] = 0
        keyboard = dict(mouse, _session_key=1, _feat_idx=None)
        listener._sessions = {0: mouse, 1: keyboard}
        listener._primary_session = 0
        listener._load_cursor(keyboard)
        listener._divert_check_due = True

        with self.assertRaises(IOError):
            listener._maybe_verify_divert(now=100.0)

        listener._sessions = {1: keyboard}
        listener._primary_session = 1
        listener._divert_check_due = True
        listener._maybe_verify_divert(now=200.0)  # nothing to verify

    def test_battery_broadcast_schedules_divert_check(self):
        listener = hid_gesture.HidGestureListener()
        listener._handle_battery_notification([])
        self.assertTrue(listener._divert_check_due)

    def test_wireless_relink_broadcast_schedules_divert_check(self):
        listener = hid_gesture.HidGestureListener()
        listener._feat_idx = 0x0D
        listener._wireless_status_idx = 0x04
        # Captured from an MX Master 4 on Bolt after a power-switch cycle.
        listener._on_report([0x11, 0x02, 0x04, 0x00, 0x01, 0x01, 0x01]
                            + [0x00] * 13)
        self.assertTrue(listener._divert_check_due)

    def test_wireless_status_without_relink_is_ignored(self):
        listener = hid_gesture.HidGestureListener()
        listener._wireless_status_idx = 0x04
        listener._on_report([0x11, 0x02, 0x04, 0x00, 0x00, 0x00, 0x00]
                            + [0x00] * 13)
        self.assertFalse(listener._divert_check_due)

    def test_own_wireless_status_response_is_ignored(self):
        listener = hid_gesture.HidGestureListener()
        listener._wireless_status_idx = 0x04
        listener._on_report([0x11, 0x02, 0x04, hid_gesture.MY_SW,
                             0x01, 0x01, 0x01] + [0x00] * 13)
        self.assertFalse(listener._divert_check_due)


class ExtraDivertLifecycleTests(unittest.TestCase):
    """Live divert refreshes, new sessions and wake replay on a keyboard."""

    CALC = 0x000A
    THUMB = 0x00C3

    def _listener(self, controls=None):
        listener = hid_gesture.HidGestureListener()
        listener._dev = object()
        listener._feat_idx = 0x09
        listener._sessions = {}
        listener._last_controls = controls if controls is not None else []
        listener._tx = Mock()
        listener._set_cid_reporting = Mock(return_value=(0,))
        return listener

    def test_live_refresh_keeps_the_thumb_button_extra(self):
        listener = self._listener()
        listener._thumb_button_cid = self.THUMB
        listener._extra_diverts = {
            self.THUMB: {"on_down": lambda: None, "on_up": lambda: None, "held": False}
        }

        listener.update_extra_diverts({})
        listener._apply_pending_extra_diverts()

        self.assertIn(self.THUMB, listener._extra_diverts)
        listener._tx.assert_not_called()

    def test_live_refresh_skips_controls_the_device_does_not_have(self):
        listener = self._listener(controls=[{"cid": 0x00E9}])
        listener._extra_diverts = {}

        listener.update_extra_diverts({
            0x00E9: {"on_down": lambda: None},
            self.CALC: {"on_down": lambda: None},   # another device's control
        })
        listener._apply_pending_extra_diverts()

        self.assertEqual(set(listener._extra_diverts), {0x00E9})
        written = [call.args[0] for call in listener._set_cid_reporting.call_args_list]
        self.assertEqual(written, [0x00E9])

    def test_refresh_updates_the_template_for_devices_bound_later(self):
        listener = hid_gesture.HidGestureListener(extra_diverts={})

        listener.update_extra_diverts({self.CALC: {"on_down": lambda: None}})
        listener._reset_device_cursor(None)

        self.assertIn(self.CALC, listener._extra_diverts)

    def test_unanswered_divert_writes_do_not_count_towards_sleep(self):
        listener = self._listener(controls=[{"cid": 0x00E9}])
        listener._extra_diverts = {}
        listener._consecutive_request_timeouts = 1

        def timed_out(cid, flags, timeout_ms=2000):
            listener._consecutive_request_timeouts += 1
            return None

        listener._set_cid_reporting = Mock(side_effect=timed_out)
        listener.update_extra_diverts({0x00E9: {"on_down": lambda: None}})
        listener._apply_pending_extra_diverts()

        self.assertEqual(listener._consecutive_request_timeouts, 1)
        self.assertEqual(
            listener._set_cid_reporting.call_args.kwargs.get("timeout_ms"), 500)

    def test_connect_drops_extras_the_device_lacks(self):
        listener = self._listener(controls=[{"cid": 0x00E9}])
        listener._extra_diverts = {
            0x00E9: {"on_down": lambda: None, "held": False},
            0x00C4: {"on_down": lambda: None, "held": False},   # mouse mode shift
        }

        listener._divert_extras()

        self.assertEqual(set(listener._extra_diverts), {0x00E9})

    def test_keyboard_wake_does_not_write_a_gesture_cid_or_reconnect(self):
        listener = self._listener(controls=[{"cid": 0x00E9}, {"cid": 0x00E8}])
        listener._extra_diverts = {0x00E9: {"on_down": lambda: None, "held": False}}
        listener._wake_reconfigure_pending = True
        listener._wake_session_key = None
        listener.force_reconnect = Mock()
        written = []
        listener._set_cid_reporting = lambda cid, flags, timeout_ms=2000: (
            written.append(cid) or ((0,) if cid in (0x00E9, 0x00E8) else None))

        self.assertTrue(listener._consume_wake_reconfiguration())

        self.assertEqual(written, [0x00E9])
        listener.force_reconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
