// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../contracts/DungeonNFT.sol";

contract DeployDungeonNFTScript is Script {
    function run() external {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        vm.startBroadcast(deployerPrivateKey);
        DungeonNFT nft = new DungeonNFT();
        console.log("DungeonNFT deployed at:", address(nft));
        vm.stopBroadcast();
    }
}
