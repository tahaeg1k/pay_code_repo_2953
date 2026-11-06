// SPDX-License-Identifier: Apache-2.0
/*
 * PayCodeEscrow — reversible stablecoin (PYUSD) payments for PayCodes.
 *
 * Design adapted from Circle's Refund Protocol (Apache-2.0, Copyright 2025 Circle Internet Group, Inc.):
 * payments are held for a merchant-specific refund window, an arbiter can settle disputes, and the
 * merchant can refund voluntarily. Deliberately simplified:
 *   - per-payment escrow, no pooled balances or debts  -> one payment's funds can never cover another's
 *   - no arbiter early-withdrawal function            -> avoids the drain issue flagged in Circle's README
 *   - the payer can freeze a payment by disputing it inside the window
 *   - each payment carries the hash of its off-chain PayCodes evidence record
 */
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

contract PayCodeEscrow is ReentrancyGuard {
    using SafeERC20 for IERC20;

    enum Status {
        None,
        Escrowed,
        Disputed,
        Released,
        Refunded
    }

    struct Payment {
        address payer;
        address merchant;
        uint96 amount;
        uint40 releaseAt;
        Status status;
        bytes32 evidenceHash;
    }

    uint256 public constant DEFAULT_REFUND_WINDOW = 3 days;
    uint256 public constant MAX_REFUND_WINDOW = 30 days;

    IERC20 public immutable token;
    address public arbiter;
    uint256 public nextPaymentId;

    mapping(uint256 => Payment) public payments;
    mapping(address => uint256) private _refundWindow;
    mapping(address => bool) private _windowSet;

    event PaymentEscrowed(
        uint256 indexed id, address indexed payer, address indexed merchant, uint256 amount, uint256 releaseAt, bytes32 evidenceHash
    );
    event PaymentDisputed(uint256 indexed id, bytes32 reasonHash);
    event PaymentReleased(uint256 indexed id, address indexed merchant, uint256 amount);
    event PaymentRefunded(uint256 indexed id, address indexed payer, uint256 amount, address indexed by);
    event RefundWindowSet(address indexed merchant, uint256 secondsWindow);
    event ArbiterChanged(address indexed oldArbiter, address indexed newArbiter);

    error ZeroAddress();
    error ZeroAmount();
    error AmountTooLarge();
    error NotPayer();
    error NotMerchant();
    error NotArbiter();
    error WrongStatus(Status current);
    error WindowClosed();
    error StillInRefundWindow(uint256 releaseAt);
    error WindowTooLong();

    modifier onlyArbiter() {
        if (msg.sender != arbiter) revert NotArbiter();
        _;
    }

    constructor(IERC20 _token, address _arbiter) {
        if (address(_token) == address(0) || _arbiter == address(0)) revert ZeroAddress();
        token = _token;
        arbiter = _arbiter;
    }

    // ---------------------------------------------------------------- payer
    /// Pay `merchant` `amount` (token base units). Funds stay in escrow until the refund window ends.
    function pay(address merchant, uint256 amount, bytes32 evidenceHash) external nonReentrant returns (uint256 id) {
        if (merchant == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (amount > type(uint96).max) revert AmountTooLarge();

        id = nextPaymentId++;
        uint256 releaseAt = block.timestamp + refundWindowOf(merchant);
        payments[id] = Payment(msg.sender, merchant, uint96(amount), uint40(releaseAt), Status.Escrowed, evidenceHash);

        token.safeTransferFrom(msg.sender, address(this), amount);
        emit PaymentEscrowed(id, msg.sender, merchant, amount, releaseAt, evidenceHash);
    }

    /// Payer freezes the payment while the refund window is open; only the arbiter or merchant can then settle it.
    function dispute(uint256 id, bytes32 reasonHash) external {
        Payment storage p = payments[id];
        if (msg.sender != p.payer) revert NotPayer();
        if (p.status != Status.Escrowed) revert WrongStatus(p.status);
        if (block.timestamp >= p.releaseAt) revert WindowClosed();
        p.status = Status.Disputed;
        emit PaymentDisputed(id, reasonHash);
    }

    // ------------------------------------------------------------- merchant
    /// Merchant refunds voluntarily (escrowed or disputed).
    function refundByMerchant(uint256 id) external nonReentrant {
        Payment storage p = payments[id];
        if (msg.sender != p.merchant) revert NotMerchant();
        if (p.status != Status.Escrowed && p.status != Status.Disputed) revert WrongStatus(p.status);
        _refund(id, p);
    }

    /// Anyone can release an undisputed payment to the merchant once the window has passed.
    function release(uint256 id) external nonReentrant {
        Payment storage p = payments[id];
        if (p.status != Status.Escrowed) revert WrongStatus(p.status);
        if (block.timestamp < p.releaseAt) revert StillInRefundWindow(p.releaseAt);
        p.status = Status.Released;
        token.safeTransfer(p.merchant, p.amount);
        emit PaymentReleased(id, p.merchant, p.amount);
    }

    // -------------------------------------------------------------- arbiter
    /// Arbiter settles a disputed payment, using the off-chain evidence trail.
    function resolve(uint256 id, bool refundPayer) external onlyArbiter nonReentrant {
        Payment storage p = payments[id];
        if (p.status != Status.Disputed) revert WrongStatus(p.status);
        if (refundPayer) {
            _refund(id, p);
        } else {
            p.status = Status.Released;
            token.safeTransfer(p.merchant, p.amount);
            emit PaymentReleased(id, p.merchant, p.amount);
        }
    }

    function setRefundWindow(address merchant, uint256 secondsWindow) external onlyArbiter {
        if (secondsWindow > MAX_REFUND_WINDOW) revert WindowTooLong();
        _refundWindow[merchant] = secondsWindow;
        _windowSet[merchant] = true;
        emit RefundWindowSet(merchant, secondsWindow);
    }

    function setArbiter(address newArbiter) external onlyArbiter {
        if (newArbiter == address(0)) revert ZeroAddress();
        emit ArbiterChanged(arbiter, newArbiter);
        arbiter = newArbiter;
    }

    // ----------------------------------------------------------------- views
    function refundWindowOf(address merchant) public view returns (uint256) {
        return _windowSet[merchant] ? _refundWindow[merchant] : DEFAULT_REFUND_WINDOW;
    }

    function getPayment(uint256 id) external view returns (Payment memory) {
        return payments[id];
    }

    // -------------------------------------------------------------- internal
    function _refund(uint256 id, Payment storage p) private {
        p.status = Status.Refunded;
        token.safeTransfer(p.payer, p.amount);
        emit PaymentRefunded(id, p.payer, p.amount, msg.sender);
    }
}
