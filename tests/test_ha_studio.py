"""Real Home Assistant API tests; run in the pinned HA image, not a fake HA."""

import asyncio
import base64
import io
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
except ImportError as error:
    raise unittest.SkipTest('Run inside the Home Assistant test image') from error

from custom_components.tinyprint import async_setup


class StudioAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        await async_setup(self.hass, {})

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.tmp.cleanup()

    async def test_new_services_always_register(self):
        for action in ('preview', 'print_document', 'print_template', 'printer_info', 'diagnose', 'cancel'):
            self.assertTrue(self.hass.services.has_service('tinyprint', action), action)

    async def test_preview_without_printer_or_bluetooth(self):
        result = await self.hass.services.async_call('tinyprint', 'preview', {'document': {'kind': 'qr', 'text': 'https://example.com'}}, blocking=True, return_response=True)
        image = Image.open(io.BytesIO(base64.b64decode(result['png_base64'])))
        self.assertEqual(image.width, 384)
        self.assertEqual(result['confirmation'], 'preview_only')
        self.assertFalse(self.hass.data['tinyprint'])

    async def test_unknown_options_are_service_validation_errors(self):
        with self.assertRaises(ServiceValidationError):
            await self.hass.services.async_call('tinyprint', 'preview', {'document': {'kind': 'qr', 'text': 'x', 'options': {'evil': 'value'}}}, blocking=True, return_response=True)

    async def test_explicit_device_routes_only_matching_printer(self):
        one = SimpleNamespace(async_print=AsyncMock(return_value={'confirmation': 'transport_only'}))
        other = SimpleNamespace(async_print=AsyncMock())
        self.hass.data['tinyprint'] = {'one': one, 'two': other}
        registry = MagicMock()
        registry.async_get.return_value = SimpleNamespace(config_entries={'one'})
        document = {'kind': 'qr', 'text': 'x'}
        with patch('custom_components.tinyprint.studio.dr.async_get', return_value=registry):
            result = await self.hass.services.async_call('tinyprint', 'print_document', {'device_id': 'chosen', 'document': document}, blocking=True, return_response=True)
        one.async_print.assert_awaited_once_with('document', {'document': document, 'expected_sha256': None}, 24)
        other.async_print.assert_not_awaited()
        self.assertEqual(result['confirmation'], 'transport_only')

    async def test_unknown_device_never_falls_back(self):
        registry = MagicMock()
        registry.async_get.return_value = None
        self.hass.data['tinyprint'] = {'one': SimpleNamespace(async_print=AsyncMock())}
        with patch('custom_components.tinyprint.studio.dr.async_get', return_value=registry):
            with self.assertRaises(ServiceValidationError):
                await self.hass.services.async_call('tinyprint', 'print_document', {'device_id': 'missing', 'document': {'kind': 'qr', 'text': 'x'}}, blocking=True)
        self.hass.data['tinyprint']['one'].async_print.assert_not_awaited()

    async def test_panel_registration_is_idempotent(self):
        from custom_components.tinyprint.studio import async_setup_panel
        self.hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
        with patch('custom_components.tinyprint.studio.panel_custom.async_register_panel', new=AsyncMock()) as register:
            await asyncio.gather(async_setup_panel(self.hass), async_setup_panel(self.hass))
            register.assert_awaited_once()
            self.assertTrue(register.call_args.kwargs['require_admin'])
            self.assertEqual(register.call_args.kwargs['frontend_url_path'], 'tinyprint')
        self.hass.http.async_register_static_paths.assert_awaited_once()
        self.hass.http = None

    async def test_loaded_entry_uses_studio_runtime_and_registers_panel(self):
        from custom_components.tinyprint import async_setup_entry
        from custom_components.tinyprint.studio_runtime import StudioPrinter
        entry = MagicMock(entry_id='entry', title='Test', data={'address': 'AA:BB:CC:DD:EE:FF'}, options={})
        entries = SimpleNamespace(async_forward_entry_setups=AsyncMock())
        with patch.object(self.hass, 'config_entries', entries), patch('custom_components.tinyprint.async_setup_panel', new=AsyncMock()) as panel:
            self.assertTrue(await async_setup_entry(self.hass, entry))
            panel.assert_awaited_once_with(self.hass)
        self.assertIsInstance(self.hass.data['tinyprint']['entry'], StudioPrinter)

    async def test_nonadmin_cannot_use_studio_actions(self):
        from homeassistant.core import Context
        from homeassistant.exceptions import Unauthorized
        auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False)))
        with patch.object(self.hass, 'auth', auth):
            with self.assertRaises(Unauthorized):
                await self.hass.services.async_call('tinyprint', 'preview', {'document': {'kind': 'qr', 'text': 'private'}}, blocking=True, return_response=True, context=Context(user_id='nonadmin'))

    async def test_runtime_failure_records_phase_without_secret_text(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter
        printer = StudioPrinter(self.hass, 'one', 'AA:BB:CC:DD:EE:FF', 'Test', None)
        with patch.object(printer, '_render', return_value=(bytes(48), 1)), patch('custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address', return_value=object()), patch('custom_components.tinyprint.studio_runtime.establish_connection', new=AsyncMock(side_effect=TimeoutError('PRIVATE CONTENT'))):
            with self.assertLogs('custom_components.tinyprint.studio_runtime', level='INFO') as logs:
                with self.assertRaises(HomeAssistantError):
                    await printer.async_print('text', 'PRIVATE CONTENT')
        self.assertEqual(printer.last_result['failure_phase'], 'connecting_gatt')
        self.assertEqual(printer.last_result['bytes_written'], 0)
        self.assertNotIn('PRIVATE CONTENT', '\n'.join(logs.output))
        self.assertEqual(printer.status, 'error')

    async def test_preview_hash_mismatch_fails_before_bluetooth(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter
        printer = StudioPrinter(self.hass, 'one', 'AA:BB:CC:DD:EE:FF', 'Test', None)
        with patch('custom_components.tinyprint.studio_runtime.establish_connection', new=AsyncMock()) as connect:
            with self.assertRaises(ServiceValidationError):
                await printer.async_print('document', {'document': {'kind': 'qr', 'text': 'x'}, 'expected_sha256': '0'*64})
        connect.assert_not_awaited()

    async def test_partial_write_counters_survive_failure(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter, TrackedClient
        printer = StudioPrinter(self.hass, 'one', 'AA:BB:CC:DD:EE:FF', 'Test', None)
        printer.begin()
        client = TrackedClient(SimpleNamespace(write_gatt_char=AsyncMock(side_effect=[None, RuntimeError('PRIVATE CONTENT')])), printer)
        await client.write_gatt_char('test', b'abc', response=False)
        with self.assertRaises(RuntimeError):
            await client.write_gatt_char('test', b'xyz', response=False)
        self.assertEqual(printer.last_result['bytes_written'], 3)
        self.assertTrue(printer.last_result['delivery_uncertain'])

    async def test_diagnose_never_sends_raster(self):
        from custom_components.tinyprint.studio_runtime import StudioPrinter
        from custom_components.tinyprint.protocol import WRITE_UUID, NOTIFY_UUID
        printer = StudioPrinter(self.hass, 'one', 'AA:BB:CC:DD:EE:FF', 'Test', None)
        chars = {WRITE_UUID: SimpleNamespace(uuid=WRITE_UUID, properties=['write-without-response']), NOTIFY_UUID: SimpleNamespace(uuid=NOTIFY_UUID, properties=['notify'])}
        class Services:
            def __iter__(self):
                return iter([SimpleNamespace(uuid='service', characteristics=list(chars.values()))])
            def get_characteristic(self, uuid):
                return chars.get(uuid)
        client = SimpleNamespace(services=Services(), is_connected=True, disconnect=AsyncMock(), write_gatt_char=AsyncMock())
        with patch('custom_components.tinyprint.studio_runtime.bluetooth.async_ble_device_from_address', return_value=object()), patch('custom_components.tinyprint.studio_runtime.establish_connection', new=AsyncMock(return_value=client)):
            result = await printer.async_diagnose()
        client.write_gatt_char.assert_not_awaited()
        client.disconnect.assert_awaited()
        self.assertEqual(result['confirmation'], 'connection_only')
        self.assertTrue(result['supported_profile'])


if __name__ == '__main__':
    unittest.main()
