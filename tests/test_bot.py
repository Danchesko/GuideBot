import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

# These credentials are never used for network requests.
os.environ.setdefault('LLM_API_KEY', 'offline-test')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123:offline-test')
from bishkek_food_finder import bot
from telegram.ext import PicklePersistence


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=1, username='tester'),
            message=SimpleNamespace(
                text='coffee', reply_text=AsyncMock(), reply_document=AsyncMock(),
                chat=SimpleNamespace(send_action=AsyncMock()),
                location=SimpleNamespace(latitude=42.87, longitude=74.60),
            ),
        )
        self.context = SimpleNamespace(user_data={})
        self.allowed = patch.object(bot, 'ALLOWED_USERS', [])
        self.allowed.start()
        self.addCleanup(self.allowed.stop)

    async def test_health_marker_requires_successful_poll(self):
        marker = self.root / 'healthy'
        instance = bot.PollingBot('123:offline-test')
        with patch.object(bot, 'HEALTH_FILE', marker):
            with patch.object(bot.ExtBot, 'get_updates', AsyncMock(return_value=())):
                self.assertEqual(await instance.get_updates(), ())
            stamp = marker.stat().st_mtime_ns
            with patch.object(bot.ExtBot, 'get_updates', AsyncMock(side_effect=RuntimeError('offline'))):
                with self.assertRaises(RuntimeError):
                    await instance.get_updates()
            self.assertEqual(marker.stat().st_mtime_ns, stamp)

    async def test_city_pending_query_location_and_reset(self):
        await bot.on_message(self.update, self.context)
        self.assertEqual(self.context.user_data['pending_message'], 'coffee')
        self.update.message.text = '🇰🇿 Алматы'
        with patch.object(bot, 'process_query', AsyncMock()) as process:
            await bot.on_message(self.update, self.context)
            process.assert_awaited_once_with(self.update, self.context, 'coffee')
        self.assertEqual(self.context.user_data['city'], 'almaty')
        await bot.on_location(self.update, self.context)
        self.assertEqual(self.context.user_data['location'], (42.87, 74.60))
        self.context.user_data['history'] = ['old']
        await bot.cmd_reset(self.update, self.context)
        self.assertEqual(self.context.user_data['history'], [])
        self.update.message.text = '🇰🇬 Бишкек'
        await bot.on_message(self.update, self.context)
        self.assertNotIn('location', self.context.user_data)

    async def test_query_updates_history_and_json_export(self):
        self.context.user_data.update(city='bishkek', location=(42.87, 74.60))
        results = {'restaurants': [{'id': 'test', 'name': 'Test'}]}
        with patch.object(bot, 'agent_run', return_value=('Answer', ['history'], results)) as run:
            await bot.process_query(self.update, self.context, 'coffee')
            self.assertIn('42.87', run.call_args.args[0])
        self.assertEqual(self.context.user_data['history'], ['history'])
        self.assertEqual(self.context.user_data['last_results'], results)
        await bot.cmd_json(self.update, self.context)
        self.update.message.reply_document.assert_awaited_once()

    async def test_persistence_round_trip(self):
        path = self.root / 'bot_persistence.pickle'
        state = {'city': 'bishkek', 'history': [{'role': 'user', 'content': 'coffee'}], 'location': (42.87, 74.60)}
        first = PicklePersistence(filepath=path)
        await first.update_user_data(1, state)
        await first.flush()
        second = PicklePersistence(filepath=path)
        self.assertEqual((await second.get_user_data())[1], state)

    async def test_unauthorized_user_does_not_run_agent(self):
        with patch.object(bot, 'ALLOWED_USERS', ['someone-else']), patch.object(bot, 'agent_run') as run:
            await bot.on_message(self.update, self.context)
            run.assert_not_called()
        self.assertEqual(self.context.user_data, {})
