// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title DungeonNFT
 * @dev ERC721 representing dungeons with on-chain traits.
 * Preminted and airdropped. Owner can be revoked later for fixed supply.
 */
contract DungeonNFT is ERC721, Ownable {
    /// @notice Dungeon theme
    enum Theme { Cave, Forest, Crypt, Ruins, Abyss, Temple, Volcano, Glacier, Swamp, Shadow }

    /// @notice Dungeon rarity
    enum Rarity { Common, Rare, Epic, Legendary }

    /// @notice On-chain dungeon traits
    struct DungeonTraits {
        uint8 difficulty;    // 1-10
        uint8 partySize;     // 2-6
        Theme theme;
        Rarity rarity;
    }

    /// @notice Current token ID counter
    uint256 public nextTokenId;

    /// @notice Token ID => Traits
    mapping(uint256 => DungeonTraits) public traits;

    /// @notice Emitted when a dungeon is minted
    event DungeonMinted(
        uint256 indexed tokenId,
        address indexed to,
        uint8 difficulty,
        uint8 partySize,
        Theme theme,
        Rarity rarity
    );

    error InvalidDifficulty();
    error InvalidPartySize();
    error TokenDoesNotExist();

    constructor() ERC721("Dungeon NFT", "DUNGEON") Ownable(msg.sender) {}

    /// @notice Mint a new dungeon with traits
    /// @param to Recipient address
    /// @param difficulty 1-10 difficulty rating
    /// @param partySize 2-6 party size required
    /// @param theme Dungeon theme
    /// @param rarity Dungeon rarity
    function mint(
        address to,
        uint8 difficulty,
        uint8 partySize,
        Theme theme,
        Rarity rarity
    ) external onlyOwner returns (uint256 tokenId) {
        if (difficulty < 1 || difficulty > 10) revert InvalidDifficulty();
        if (partySize < 2 || partySize > 6) revert InvalidPartySize();

        tokenId = nextTokenId++;
        
        // Set traits BEFORE _safeMint to prevent reentrancy issues
        traits[tokenId] = DungeonTraits({
            difficulty: difficulty,
            partySize: partySize,
            theme: theme,
            rarity: rarity
        });
        
        _safeMint(to, tokenId);

        emit DungeonMinted(tokenId, to, difficulty, partySize, theme, rarity);
    }

    /// @notice Batch mint dungeons
    /// @param recipients Array of recipient addresses
    /// @param difficulties Array of difficulties
    /// @param partySizes Array of party sizes
    /// @param themes Array of themes
    /// @param rarities Array of rarities
    function batchMint(
        address[] calldata recipients,
        uint8[] calldata difficulties,
        uint8[] calldata partySizes,
        Theme[] calldata themes,
        Rarity[] calldata rarities
    ) external onlyOwner {
        uint256 length = recipients.length;
        require(
            length == difficulties.length &&
            length == partySizes.length &&
            length == themes.length &&
            length == rarities.length,
            "Array length mismatch"
        );

        for (uint256 i = 0; i < length; i++) {
            if (difficulties[i] < 1 || difficulties[i] > 10) revert InvalidDifficulty();
            if (partySizes[i] < 2 || partySizes[i] > 6) revert InvalidPartySize();

            uint256 tokenId = nextTokenId++;
            
            // Set traits BEFORE _safeMint to prevent reentrancy issues
            traits[tokenId] = DungeonTraits({
                difficulty: difficulties[i],
                partySize: partySizes[i],
                theme: themes[i],
                rarity: rarities[i]
            });
            
            _safeMint(recipients[i], tokenId);

            emit DungeonMinted(
                tokenId,
                recipients[i],
                difficulties[i],
                partySizes[i],
                themes[i],
                rarities[i]
            );
        }
    }

    /// @notice Get traits for a dungeon
    /// @param tokenId Token ID to query
    function getTraits(uint256 tokenId) external view returns (DungeonTraits memory) {
        if (_ownerOf(tokenId) == address(0)) revert TokenDoesNotExist();
        return traits[tokenId];
    }

    /// @notice Get the max gold cap for a dungeon based on difficulty
    /// @param tokenId Token ID to query
    /// @param baseGoldRate Base gold rate per difficulty point
    function getMaxGold(uint256 tokenId, uint256 baseGoldRate) external view returns (uint256) {
        if (_ownerOf(tokenId) == address(0)) revert TokenDoesNotExist();
        return uint256(traits[tokenId].difficulty) * baseGoldRate;
    }
}
