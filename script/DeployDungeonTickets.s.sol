// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../contracts/DungeonTickets.sol";

contract DeployDungeonTicketsScript is Script {
    address constant GOLD = 0x004AdCd5C3925f83665718e6Ec8CCe4dB0B24aaD;
    
    function run() external {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        vm.startBroadcast(deployerPrivateKey);
        DungeonTickets tickets = new DungeonTickets(GOLD, 100 ether);
        console.log("DungeonTickets deployed at:", address(tickets));
        vm.stopBroadcast();
    }
}
