# PayCodeEscrow — reversible PYUSD payments

Stablecoin transfers are final. This escrow makes PayCode payments reversible for a refund window:

1. **pay(merchant, amount, evidenceHash)**: payer's PYUSD goes into escrow, tagged with the hash of the PayCodes evidence record.
2. **dispute(id, reasonHash)**: payer can freeze it while the window is open (default 3 days, set per merchant, max 30).
3. **resolve(id, refundPayer)**: arbiter settles a dispute using the evidence trail.
4. **refundByMerchant(id)**: merchant refunds voluntarily at any point before release.
5. **release(id)**: anyone releases an undisputed payment to the merchant after the window.

Adapted from Circle's Refund Protocol (Apache-2.0). Differences: per-payment escrow (no pooled balances/debts),
no arbiter early-withdrawal function (Circle's README flags a drain issue there), payer-initiated disputes,
evidence hash on every payment.

## Test
```bash
cd ~/Code/hackathons/paypal/contracts/paycode-escrow
forge test -vv
```

## Deploy to Ethereum Sepolia (testnet, no real value)
PYUSD testnet token: `0xCaC524BcA292aaade2DF8A05cC58F0a65B1B3bB9` (Paxos docs). Test PYUSD: https://faucet.paxos.com/
```bash
export SEPOLIA_RPC_URL=...  DEPLOYER_KEY=...   # testnet-only key
forge script script/Deploy.s.sol --rpc-url $SEPOLIA_RPC_URL --private-key $DEPLOYER_KEY --broadcast
```

## Try it with cast
```bash
ESC=<deployed address>; PYUSD=0xCaC524BcA292aaade2DF8A05cC58F0a65B1B3bB9
cast send $PYUSD "approve(address,uint256)" $ESC 10000000 --private-key $PAYER_KEY --rpc-url $SEPOLIA_RPC_URL
cast send $ESC "pay(address,uint256,bytes32)" $MERCHANT 5000000 $(cast keccak EV-XXXXXXXX) --private-key $PAYER_KEY --rpc-url $SEPOLIA_RPC_URL
cast send $ESC "dispute(uint256,bytes32)" 0 $(cast keccak "not delivered") --private-key $PAYER_KEY --rpc-url $SEPOLIA_RPC_URL
cast send $ESC "resolve(uint256,bool)" 0 true --private-key $ARBITER_KEY --rpc-url $SEPOLIA_RPC_URL
```
