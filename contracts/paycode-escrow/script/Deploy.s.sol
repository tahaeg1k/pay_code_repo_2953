// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/PayCodeEscrow.sol";

/// forge script script/Deploy.s.sol --rpc-url $SEPOLIA_RPC_URL --private-key $DEPLOYER_KEY --broadcast
contract Deploy is Script {
    function run() external returns (PayCodeEscrow esc) {
        address pyusd = vm.envOr("PYUSD_ADDRESS", address(0xCaC524BcA292aaade2DF8A05cC58F0a65B1B3bB9)); // PYUSD on Ethereum Sepolia
        address arbiter = vm.envOr("ARBITER_ADDRESS", msg.sender);
        vm.startBroadcast();
        esc = new PayCodeEscrow(IERC20(pyusd), arbiter);
        vm.stopBroadcast();
        console.log("PayCodeEscrow deployed at", address(esc));
    }
}
