// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/token/ERC20/extensions/ERC20Burnable.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title Gold
 * @dev ERC20 token for the Dungeon game. Minted only by DungeonManager.
 * Burnable by anyone (used for tickets, artifacts, etc.)
 */
contract Gold is ERC20, ERC20Burnable, Ownable {
    /// @notice Address authorized to mint gold (DungeonManager)
    address public minter;

    /// @notice Emitted when minter is set
    event MinterSet(address indexed oldMinter, address indexed newMinter);

    error OnlyMinter();
    error ZeroAddress();

    constructor() ERC20("Dungeon Gold", "GOLD") Ownable(msg.sender) {}

    /// @notice Set the minter address (typically DungeonManager)
    /// @param _minter Address authorized to mint
    function setMinter(address _minter) external onlyOwner {
        if (_minter == address(0)) revert ZeroAddress();
        emit MinterSet(minter, _minter);
        minter = _minter;
    }

    /// @notice Mint gold (only callable by minter)
    /// @param to Recipient address
    /// @param amount Amount to mint
    function mint(address to, uint256 amount) external {
        if (msg.sender != minter) revert OnlyMinter();
        _mint(to, amount);
    }
}
