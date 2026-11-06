// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../src/PayCodeEscrow.sol";
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// 6-decimal stand-in for PYUSD
contract MockPYUSD is ERC20 {
    constructor() ERC20("PayPal USD (mock)", "PYUSD") {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

contract PayCodeEscrowTest is Test {
    PayCodeEscrow esc;
    MockPYUSD pyusd;
    address arbiter = makeAddr("arbiter");
    address agent = makeAddr("agentWallet"); // payer (e.g. the AI agent's wallet)
    address greenleaf = makeAddr("greenleaf");
    address northside = makeAddr("northside");
    bytes32 constant EV = keccak256("EV-82504E3D");
    uint256 constant AMT = 25_500_000; // 25.50 PYUSD

    function setUp() public {
        pyusd = new MockPYUSD();
        esc = new PayCodeEscrow(pyusd, arbiter);
        pyusd.mint(agent, 1_000e6);
        vm.prank(agent);
        pyusd.approve(address(esc), type(uint256).max);
    }

    function _pay(address m, uint256 a) internal returns (uint256 id) {
        vm.prank(agent);
        id = esc.pay(m, a, EV);
    }

    function test_PayEscrowsFunds() public {
        uint256 id = _pay(greenleaf, AMT);
        PayCodeEscrow.Payment memory p = esc.getPayment(id);
        assertEq(p.payer, agent);
        assertEq(p.merchant, greenleaf);
        assertEq(p.amount, AMT);
        assertEq(p.evidenceHash, EV);
        assertEq(uint8(p.status), uint8(PayCodeEscrow.Status.Escrowed));
        assertEq(p.releaseAt, block.timestamp + 3 days);
        assertEq(pyusd.balanceOf(address(esc)), AMT);
        assertEq(pyusd.balanceOf(greenleaf), 0);
    }

    function test_ReleaseOnlyAfterWindow() public {
        uint256 id = _pay(greenleaf, AMT);
        vm.expectRevert(abi.encodeWithSelector(PayCodeEscrow.StillInRefundWindow.selector, block.timestamp + 3 days));
        esc.release(id);
        vm.warp(block.timestamp + 3 days);
        esc.release(id); // anyone can trigger
        assertEq(pyusd.balanceOf(greenleaf), AMT);
        vm.expectRevert(abi.encodeWithSelector(PayCodeEscrow.WrongStatus.selector, PayCodeEscrow.Status.Released));
        esc.release(id);
    }

    function test_DisputeBlocksReleaseAndArbiterRefunds() public {
        uint256 id = _pay(greenleaf, AMT);
        uint256 before = pyusd.balanceOf(agent);
        vm.prank(agent);
        esc.dispute(id, keccak256("item not delivered"));
        vm.warp(block.timestamp + 10 days);
        vm.expectRevert(abi.encodeWithSelector(PayCodeEscrow.WrongStatus.selector, PayCodeEscrow.Status.Disputed));
        esc.release(id);
        vm.prank(arbiter);
        esc.resolve(id, true);
        assertEq(pyusd.balanceOf(agent), before + AMT);
        assertEq(pyusd.balanceOf(address(esc)), 0);
    }

    function test_ArbiterCanSideWithMerchant() public {
        uint256 id = _pay(greenleaf, AMT);
        vm.prank(agent);
        esc.dispute(id, bytes32(0));
        vm.prank(arbiter);
        esc.resolve(id, false);
        assertEq(pyusd.balanceOf(greenleaf), AMT);
    }

    function test_MerchantVoluntaryRefund() public {
        uint256 id = _pay(greenleaf, AMT);
        vm.prank(greenleaf);
        esc.refundByMerchant(id);
        assertEq(pyusd.balanceOf(agent), 1_000e6);
        vm.prank(greenleaf);
        vm.expectRevert(abi.encodeWithSelector(PayCodeEscrow.WrongStatus.selector, PayCodeEscrow.Status.Refunded));
        esc.refundByMerchant(id);
    }

    function test_OnlyPayerCanDispute_AndOnlyInsideWindow() public {
        uint256 id = _pay(greenleaf, AMT);
        vm.prank(greenleaf);
        vm.expectRevert(PayCodeEscrow.NotPayer.selector);
        esc.dispute(id, bytes32(0));
        vm.warp(block.timestamp + 3 days);
        vm.prank(agent);
        vm.expectRevert(PayCodeEscrow.WindowClosed.selector);
        esc.dispute(id, bytes32(0));
    }

    function test_AccessControl() public {
        uint256 id = _pay(greenleaf, AMT);
        vm.prank(agent);
        esc.dispute(id, bytes32(0));
        vm.expectRevert(PayCodeEscrow.NotArbiter.selector);
        esc.resolve(id, true);
        vm.prank(northside);
        vm.expectRevert(PayCodeEscrow.NotMerchant.selector);
        esc.refundByMerchant(id);
        vm.expectRevert(PayCodeEscrow.NotArbiter.selector);
        esc.setRefundWindow(greenleaf, 1 days);
    }

    function test_ArbiterCannotResolveUndisputed() public {
        uint256 id = _pay(greenleaf, AMT);
        vm.prank(arbiter);
        vm.expectRevert(abi.encodeWithSelector(PayCodeEscrow.WrongStatus.selector, PayCodeEscrow.Status.Escrowed));
        esc.resolve(id, false);
    }

    function test_PerMerchantWindow() public {
        vm.prank(arbiter);
        esc.setRefundWindow(northside, 1 hours);
        uint256 id = _pay(northside, AMT);
        assertEq(esc.getPayment(id).releaseAt, block.timestamp + 1 hours);
        vm.prank(arbiter);
        vm.expectRevert(PayCodeEscrow.WindowTooLong.selector);
        esc.setRefundWindow(northside, 31 days);
        vm.prank(arbiter);
        esc.setRefundWindow(greenleaf, 0); // instant settlement merchant
        uint256 id2 = _pay(greenleaf, AMT);
        esc.release(id2);
        assertEq(pyusd.balanceOf(greenleaf), AMT);
    }

    /// Refunding one payment can never touch another payment's funds.
    function test_PaymentsAreIsolated() public {
        uint256 a = _pay(greenleaf, AMT);
        uint256 b = _pay(greenleaf, 10e6);
        vm.prank(agent);
        esc.dispute(a, bytes32(0));
        vm.prank(arbiter);
        esc.resolve(a, true);
        assertEq(pyusd.balanceOf(address(esc)), 10e6);
        vm.warp(block.timestamp + 3 days);
        esc.release(b);
        assertEq(pyusd.balanceOf(greenleaf), 10e6);
        assertEq(pyusd.balanceOf(address(esc)), 0);
    }

    function testFuzz_Conservation(uint96 amount, bool disputeIt, bool refundIt) public {
        amount = uint96(bound(amount, 1, 1_000e6));
        uint256 total = pyusd.totalSupply();
        uint256 id = _pay(greenleaf, amount);
        if (disputeIt) {
            vm.prank(agent);
            esc.dispute(id, bytes32(0));
            vm.prank(arbiter);
            esc.resolve(id, refundIt);
        } else {
            vm.warp(block.timestamp + 3 days);
            esc.release(id);
        }
        assertEq(pyusd.balanceOf(address(esc)), 0);
        assertEq(pyusd.balanceOf(agent) + pyusd.balanceOf(greenleaf), total);
    }
}
