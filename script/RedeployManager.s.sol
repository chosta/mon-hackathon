// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../contracts/Gold.sol";
import "../contracts/DungeonNFT.sol";
import "../contracts/DungeonTickets.sol";
import "../contracts/DungeonManager.sol";

contract RedeployManagerScript is Script {
    // Existing contract addresses (DO NOT REDEPLOY THESE)
    address constant GOLD = 0xC8c79A0e20E3F6baBe919236c029D0e82B2c7f2d;
    address constant DUNGEON_NFT = 0x036cc7113810DF5fEFC4034FaE37cf91Ee6AeD9F;
    address constant DUNGEON_TICKETS = 0xD4005AAb88957Ff956f1c1551235bEd6515Fac2d;

    function run() external {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");

        vm.startBroadcast(deployerPrivateKey);

        // 1. Deploy new DungeonManager pointing to existing contracts
        DungeonManager manager = new DungeonManager(GOLD, DUNGEON_NFT, DUNGEON_TICKETS);
        console.log("New DungeonManager deployed at:", address(manager));

        // 2. Reconfigure permissions
        Gold(GOLD).setMinter(address(manager));
        console.log("Gold minter updated to new DungeonManager");

        DungeonTickets(DUNGEON_TICKETS).setBurner(address(manager));
        console.log("Tickets burner updated to new DungeonManager");

        // 3. Verify
        console.log("isConfigured:", manager.isConfigured());

        vm.stopBroadcast();
    }
}
