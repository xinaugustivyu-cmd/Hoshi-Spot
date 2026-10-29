"""纸上执行边界的隔离进程测试；所有调用在网络层前被拒绝。"""

import subprocess
import sys
import unittest


class RealOrderBoundaryTests(unittest.TestCase):
    def test_sync_async_ccxt_and_freqtrade_real_order_paths_are_blocked(self):
        code = r'''
import asyncio
import ccxt
import ccxt.async_support
from hoshi_v11.order_guard import RealOrderBlocked, install, is_installed
from freqtrade.exchange.exchange import Exchange as FreqtradeExchange
install()
assert is_installed()
sync = ccxt.binance()
for call in (
    lambda: sync.request('order', 'private', 'POST'),
    lambda: sync.request('order', 'public', 'GET'),
    lambda: sync.fetch2('order', 'private', 'POST'),
    lambda: sync.fetch('https://api.binance.com/api/v3/order', 'POST'),
    lambda: sync.fetch('https://api.binance.com/sapi/v1/capital/withdraw/apply', 'POST'),
):
    try:
        call()
    except RealOrderBlocked:
        pass
    else:
        raise AssertionError('real endpoint reached')
async def check_async():
    client = ccxt.async_support.binance()
    for args in (('order','private','POST'), ('account','private','GET')):
        try:
            await client.request(*args)
        except RealOrderBlocked:
            pass
        else:
            raise AssertionError('async private endpoint reached')
asyncio.run(check_async())
fake = type('Fake', (), {'_config': {'dry_run': False}})()
try:
    FreqtradeExchange.create_order(fake, pair='BTC/USDT', ordertype='limit',
                                   side='buy', amount=0.1, rate=100, leverage=1)
except RealOrderBlocked:
    pass
else:
    raise AssertionError('Freqtrade real order reached')
print('all private/order calls blocked before network')
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all private/order calls blocked", result.stdout)


if __name__ == "__main__":
    unittest.main()
