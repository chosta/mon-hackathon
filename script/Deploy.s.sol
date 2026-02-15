// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../contracts/Gold.sol";
import "../contracts/DungeonNFT.sol";
import "../contracts/DungeonTickets.sol";
import "../contracts/DungeonManager.sol";

contract DeployScript is Script {
    function run() external {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address deployer = vm.addr(deployerPrivateKey);
        
        vm.startBroadcast(deployerPrivateKey);

        // 1. Deploy Gold
        Gold gold = new Gold();
        console.log("Gold deployed at:", address(gold));

        // 2. Deploy DungeonNFT
        DungeonNFT dungeonNFT = new DungeonNFT();
        console.log("DungeonNFT deployed at:", address(dungeonNFT));

        // 3. Deploy DungeonTickets (100 gold per ticket)
        DungeonTickets tickets = new DungeonTickets(address(gold), 100 ether);
        console.log("DungeonTickets deployed at:", address(tickets));

        // 4. Deploy DungeonManager
        DungeonManager manager = new DungeonManager(
            address(gold),
            address(dungeonNFT),
            address(tickets)
        );
        console.log("DungeonManager deployed at:", address(manager));

        // 5. Configure permissions
        gold.setMinter(address(manager));
        console.log("Gold minter set to DungeonManager");

        tickets.setBurner(address(manager));
        console.log("Tickets burner set to DungeonManager");

        // 6. Mint 2 test dungeons
        dungeonNFT.mint(
            deployer,
            5,  // difficulty
            2,  // partySize
            DungeonNFT.Theme.Cave,
            DungeonNFT.Rarity.Common
        );
        console.log("Dungeon 0 minted: Cave, difficulty 5, party 2");

        dungeonNFT.mint(
            deployer,
            8,  // difficulty
            3,  // partySize
            DungeonNFT.Theme.Crypt,
            DungeonNFT.Rarity.Rare
        );
        console.log("Dungeon 1 minted: Crypt, difficulty 8, party 3");

        // 7. Mint some tickets for testing
        tickets.mint(deployer, 10);
        console.log("10 test tickets minted to deployer");

        vm.stopBroadcast();

        // Summary
        console.log("\n=== Deployment Summary ===");
        console.log("Gold:", address(gold));
        console.log("DungeonNFT:", address(dungeonNFT));
        console.log("DungeonTickets:", address(tickets));
        console.log("DungeonManager:", address(manager));
    }
}
