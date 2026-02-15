// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC1155/ERC1155.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "./Gold.sol";

/**
 * @title DungeonTickets
 * @dev ERC1155 entry tickets for dungeons.
 * Burned on entry. Purchasable by burning Gold.
 * Owner can mint for airdrops (revocable later).
 */
contract DungeonTickets is ERC1155, Ownable {
    /// @notice Standard ticket token ID
    uint256 public constant STANDARD_TICKET = 0;

    /// @notice Gold token contract
    Gold public immutable gold;

    /// @notice Cost in Gold per ticket
    uint256 public ticketPrice;

    /// @notice Address authorized to burn tickets (DungeonManager)
    address public burner;

    /// @notice Total tickets minted (for stats)
    uint256 public totalMinted;

    /// @notice Total tickets burned (for stats)
    uint256 public totalBurned;

    /// @notice Emitted when tickets are purchased with gold
    event TicketsPurchased(address indexed buyer, uint256 amount, uint256 goldBurned);

    /// @notice Emitted when ticket price is updated
    event TicketPriceUpdated(uint256 oldPrice, uint256 newPrice);

    /// @notice Emitted when burner is set
    event BurnerSet(address indexed oldBurner, address indexed newBurner);

    error InsufficientGold();
    error OnlyBurner();
    error ZeroAmount();
    error ZeroAddress();
    error PriceTooHigh();

    constructor(address _gold, uint256 _ticketPrice) ERC1155("") Ownable(msg.sender) {
        if (_gold == address(0)) revert ZeroAddress();
        gold = Gold(_gold);
        ticketPrice = _ticketPrice;
    }

    /// @notice Set the burner address (typically DungeonManager)
    /// @param _burner Address authorized to burn tickets
    function setBurner(address _burner) external onlyOwner {
        if (_burner == address(0)) revert ZeroAddress();
        emit BurnerSet(burner, _burner);
        burner = _burner;
    }

    /// @notice Update ticket price
    /// @param _ticketPrice New price in Gold (max 1e24)
    function setTicketPrice(uint256 _ticketPrice) external onlyOwner {
        if (_ticketPrice > 1e24) revert PriceTooHigh();
        emit TicketPriceUpdated(ticketPrice, _ticketPrice);
        ticketPrice = _ticketPrice;
    }

    /// @notice Purchase tickets by burning Gold
    /// @param amount Number of tickets to purchase
    function purchaseWithGold(uint256 amount) external {
        if (amount == 0) revert ZeroAmount();
        uint256 cost = amount * ticketPrice;
        
        // Burn gold from buyer (requires approval)
        gold.burnFrom(msg.sender, cost);
        
        // Mint tickets
        _mint(msg.sender, STANDARD_TICKET, amount, "");
        totalMinted += amount;

        emit TicketsPurchased(msg.sender, amount, cost);
    }

    /// @notice Mint tickets (owner only, for airdrops)
    /// @param to Recipient address
    /// @param amount Number of tickets
    function mint(address to, uint256 amount) external onlyOwner {
        if (amount == 0) revert ZeroAmount();
        _mint(to, STANDARD_TICKET, amount, "");
        totalMinted += amount;
    }

    /// @notice Batch mint to multiple addresses
    /// @param recipients Array of recipient addresses
    /// @param amounts Array of amounts
    function batchMint(address[] calldata recipients, uint256[] calldata amounts) external onlyOwner {
        require(recipients.length == amounts.length, "Array length mismatch");
        for (uint256 i = 0; i < recipients.length; i++) {
            if (amounts[i] > 0) {
                _mint(recipients[i], STANDARD_TICKET, amounts[i], "");
                totalMinted += amounts[i];
            }
        }
    }

    /// @notice Burn a ticket (only burner can call)
    /// @param from Address to burn from
    /// @param amount Number of tickets to burn
    function burnTicket(address from, uint256 amount) external {
        if (msg.sender != burner) revert OnlyBurner();
        _burn(from, STANDARD_TICKET, amount);
        totalBurned += amount;
    }

    /// @notice Get ticket balance
    /// @param account Address to check
    function ticketBalance(address account) external view returns (uint256) {
        return balanceOf(account, STANDARD_TICKET);
    }
}
