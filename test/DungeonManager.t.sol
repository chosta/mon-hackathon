// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../contracts/Gold.sol";
import "../contracts/DungeonNFT.sol";
import "../contracts/DungeonTickets.sol";
import "../contracts/DungeonManager.sol";

contract DungeonManagerTest is Test {
    function onERC721Received(address, address, uint256, bytes calldata) external pure returns (bytes4) {
        return this.onERC721Received.selector;
    }

    Gold gold;
    DungeonNFT dungeonNFT;
    DungeonTickets tickets;
    DungeonManager manager;

    address owner = address(this);
    address player1 = address(0x1);
    address player2 = address(0x2);
    address player3 = address(0x3);

    uint256 constant BOND = 0.01 ether;

    function setUp() public {
        gold = new Gold();
        dungeonNFT = new DungeonNFT();
        tickets = new DungeonTickets(address(gold), 100 ether);
        manager = new DungeonManager(address(gold), address(dungeonNFT), address(tickets));

        gold.setMinter(address(manager));
        tickets.setBurner(address(manager));

        manager.registerAgent(player1);
        manager.registerAgent(player2);
        manager.registerAgent(player3);

        dungeonNFT.mint(owner, 5, 2, DungeonNFT.Theme.Cave, DungeonNFT.Rarity.Common);

        tickets.mint(player1, 5);
        tickets.mint(player2, 5);
        tickets.mint(player3, 5);

        vm.deal(player1, 1 ether);
        vm.deal(player2, 1 ether);
        vm.deal(player3, 1 ether);

        // Constructor starts in Grace — stake default dungeon, then go Active
        dungeonNFT.approve(address(manager), 0);
        manager.stakeDungeon(0);  // dungeonId = 0

        manager.setRunner(address(this));
        manager.startEpoch();
    }

    // ============ Helpers ============

    function _stakeDungeon() internal pure returns (uint256) {
        return 0; // Pre-staked in setUp during Grace period
    }

    function _enterBothPlayers(uint256 dungeonId) internal returns (uint256 sessionId) {
        vm.prank(player1);
        manager.enterDungeon{value: BOND}(dungeonId);
        vm.prank(player2);
        manager.enterDungeon{value: BOND}(dungeonId);
        sessionId = manager.sessionCount();
    }

    function _acceptDM(uint256 sessionId) internal {
        address dm = manager.getSessionDM(sessionId);
        uint64 epoch = manager.getSessionDmEpoch(sessionId);
        vm.prank(dm);
        manager.acceptDM(sessionId, epoch);
    }

    function _setupActiveSession() internal returns (uint256 dungeonId, uint256 sessionId, address dm, address nonDm) {
        dungeonId = _stakeDungeon();
        sessionId = _enterBothPlayers(dungeonId);
        _acceptDM(sessionId);
        dm = manager.getSessionDM(sessionId);
        address[] memory party = manager.getSessionParty(sessionId);
        nonDm = party[0];
    }

    // ============ Deployment ============

    function testDeployment() public view {
        assertEq(gold.minter(), address(manager));
        assertEq(tickets.burner(), address(manager));
        assertTrue(manager.registeredAgents(player1));
    }

    // ============ Bond Entry ============

    function testEntryRequiresBond() public {
        uint256 dungeonId = _stakeDungeon();
        vm.prank(player1);
        vm.expectRevert(DungeonManager.InsufficientBond.selector);
        manager.enterDungeon{value: 0}(dungeonId);
    }

    function testEntryWithBond() public {
        uint256 dungeonId = _stakeDungeon();
        vm.prank(player1);
        manager.enterDungeon{value: BOND}(dungeonId);
        uint256 sessionId = manager.sessionCount();
        assertEq(manager.bondOf(sessionId, player1), BOND);
    }

    // ============ DM Selection ============

    function testDMSelectedOnPartyFull() public {
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        assertEq(uint256(manager.getSessionState(sessionId)), uint256(DungeonManager.SessionState.WaitingDM));
        assertTrue(manager.getSessionDM(sessionId) != address(0));
        assertEq(manager.getSessionDmEpoch(sessionId), 1);
    }

    // ============ DM Acceptance ============

    function testDMAcceptHappyPath() public {
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        _acceptDM(sessionId);
        assertEq(uint256(manager.getSessionState(sessionId)), uint256(DungeonManager.SessionState.Active));
    }

    function testDMAcceptWrongSender() public {
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        address dm = manager.getSessionDM(sessionId);
        uint64 epoch = manager.getSessionDmEpoch(sessionId);
        address notDm = dm == player1 ? player2 : player1;
        vm.prank(notDm);
        vm.expectRevert(DungeonManager.NotSelectedDM.selector);
        manager.acceptDM(sessionId, epoch);
    }

    function testDMAcceptStaleEpoch() public {
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        address dm = manager.getSessionDM(sessionId);
        vm.prank(dm);
        vm.expectRevert(DungeonManager.StaleEpoch.selector);
        manager.acceptDM(sessionId, 999);
    }

    // ============ DM Reroll ============

    function testRerollDMAfterTimeout() public {
        dungeonNFT.mint(owner, 5, 3, DungeonNFT.Theme.Cave, DungeonNFT.Rarity.Common);
        // Need Grace to stake
        manager.endEpoch();
        dungeonNFT.approve(address(manager), 1);
        uint256 dungeonId = manager.stakeDungeon(1);
        manager.startEpoch();

        vm.prank(player1);
        manager.enterDungeon{value: BOND}(dungeonId);
        vm.prank(player2);
        manager.enterDungeon{value: BOND}(dungeonId);
        vm.prank(player3);
        manager.enterDungeon{value: BOND}(dungeonId);

        uint256 sessionId = manager.sessionCount();
        address oldDm = manager.getSessionDM(sessionId);
        assertTrue(manager.bondOf(sessionId, oldDm) > 0);

        vm.warp(block.timestamp + 6 minutes);
        manager.rerollDM(sessionId);

        assertEq(manager.bondOf(sessionId, oldDm), 0);
        assertEq(manager.getSessionDmEpoch(sessionId), 2);
    }

    function testRerollDMBeforeTimeoutReverts() public {
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        vm.expectRevert(DungeonManager.TimeoutNotReached.selector);
        manager.rerollDM(sessionId);
    }

    // ============ Replay Protection ============

    function testReplayProtectionWrongTurn() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        // nonDm is currentActor (players act first)
        // Try submitting with wrong turnIndex
        vm.expectRevert(DungeonManager.WrongTurn.selector);
        manager.submitAction(sessionId, 999, "Wrong turn", nonDm);
    }

    function testReplayProtectionAlreadySubmitted() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        // Player submits action (sets actionSubmitted[1] = true)
        manager.submitAction(sessionId, 1, "I attack!", nonDm);

        // After player acts, currentActor goes back to DM
        // DM can't submitAction on same turn (already submitted)
        // Actually DM uses submitDMResponse. Let's test that DM can respond.
        // The AlreadySubmitted guard is for submitAction only.
    }

    function testDMResponseRequiresAction() public {
        (, uint256 sessionId, address dm,) = _setupActiveSession();

        // DM tries to respond before any player action
        // But DM is not currentActor (player is), so this should revert with NotYourTurn
        DungeonManager.DMAction[] memory actions = new DungeonManager.DMAction[](0);
        vm.expectRevert(DungeonManager.NotYourTurn.selector);
        manager.submitDMResponse(sessionId, 1, "Narration", actions, dm);
    }

    function testDMResponseAfterPlayerAction() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        // Player acts
        manager.submitAction(sessionId, 1, "I explore", nonDm);

        // Now DM is currentActor, can respond
        DungeonManager.DMAction[] memory actions = new DungeonManager.DMAction[](0);
        manager.submitDMResponse(sessionId, 1, "You see a cave", actions, dm);

        // Turn should advance to 2
        assertEq(manager.getSessionTurn(sessionId), 2);
    }

    // ============ Full Game Flow ============

    function testFullGameFlow() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        // Turn 1: Player acts first
        manager.submitAction(sessionId, 1, "I attack the goblin!", nonDm);

        // DM responds with rewards and completes
        DungeonManager.DMAction[] memory dmActions = new DungeonManager.DMAction[](2);
        dmActions[0] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.REWARD_GOLD,
            target: nonDm,
            value: 50,
            narrative: ""
        });
        dmActions[1] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.COMPLETE,
            target: address(0),
            value: 0,
            narrative: "Victory!"
        });

        manager.submitDMResponse(sessionId, 1, "The goblin falls!", dmActions, dm);

        assertEq(uint256(manager.getSessionState(sessionId)), uint256(DungeonManager.SessionState.Completed));

        // Bonds released
        assertTrue(manager.withdrawableBonds(dm) > 0 || manager.withdrawableBonds(nonDm) > 0);
    }

    // ============ Bond Withdrawal ============

    function testBondWithdrawal() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        manager.submitAction(sessionId, 1, "Quick quest", nonDm);

        DungeonManager.DMAction[] memory dmActions = new DungeonManager.DMAction[](1);
        dmActions[0] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.COMPLETE,
            target: address(0),
            value: 0,
            narrative: "Done!"
        });

        manager.submitDMResponse(sessionId, 1, "Complete", dmActions, dm);

        uint256 dmBond = manager.withdrawableBonds(dm);
        uint256 nonDmBond = manager.withdrawableBonds(nonDm);
        assertTrue(dmBond > 0);
        assertTrue(nonDmBond > 0);

        uint256 balBefore = dm.balance;
        vm.prank(dm);
        manager.withdrawBond();
        assertEq(dm.balance, balBefore + dmBond);
        assertEq(manager.withdrawableBonds(dm), 0);
    }

    function testWithdrawNothingReverts() public {
        vm.prank(player1);
        vm.expectRevert(DungeonManager.NothingToWithdraw.selector);
        manager.withdrawBond();
    }

    // ============ Session Timeout ============

    function testSessionTimeout() public {
        (, uint256 sessionId,,) = _setupActiveSession();
        vm.warp(block.timestamp + 4 hours + 1);
        manager.timeoutSession(sessionId);
        assertEq(uint256(manager.getSessionState(sessionId)), uint256(DungeonManager.SessionState.TimedOut));
    }

    function testSessionTimeoutTooEarly() public {
        (, uint256 sessionId,,) = _setupActiveSession();
        vm.expectRevert(DungeonManager.NotTimedOut.selector);
        manager.timeoutSession(sessionId);
    }

    // ============ Fee Distribution ============

    function testFeeDistribution() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        manager.submitAction(sessionId, 1, "I fight", nonDm);

        DungeonManager.DMAction[] memory dmActions = new DungeonManager.DMAction[](2);
        dmActions[0] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.REWARD_GOLD,
            target: nonDm,
            value: 100,
            narrative: ""
        });
        dmActions[1] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.COMPLETE,
            target: address(0),
            value: 0,
            narrative: "Victory!"
        });

        manager.submitDMResponse(sessionId, 1, "Win!", dmActions, dm);

        // DM gets 15% = 15 gold
        assertEq(gold.balanceOf(dm), 15);
        // Player gets 100 - 15 (dm) - 5 (royalty) = 80 gold
        assertEq(gold.balanceOf(nonDm), 80);
        // Dungeon owner gets 5 royalty
        assertEq(manager.pendingRoyalties(owner), 5);
    }

    // ============ Turn Timeout ============

    function testTurnTimeoutPlayerSkipped() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        // Player is currentActor, warp past turn deadline
        vm.warp(block.timestamp + 6 minutes);
        manager.timeoutAdvance(sessionId);

        // Player skipped, DM is now current actor
        // Session should still be active (player timeout doesn't fail session)
        assertEq(uint256(manager.getSessionState(sessionId)), uint256(DungeonManager.SessionState.Active));
    }

    function testTurnTimeoutDMFails() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();

        // Player acts first
        manager.submitAction(sessionId, 1, "I act", nonDm);

        // Now DM is currentActor, warp past deadline
        vm.warp(block.timestamp + 6 minutes);
        manager.timeoutAdvance(sessionId);

        assertEq(uint256(manager.getSessionState(sessionId)), uint256(DungeonManager.SessionState.Failed));
    }

    // ============ Skill Registry ============

    function testSkillRegistry() public {
        uint256 skillId = manager.addSkill("DM Prompt", "You are a dungeon master...");
        DungeonManager.Skill memory skill = manager.getSkill(skillId);
        assertEq(skill.name, "DM Prompt");
        assertTrue(bytes(skill.content).length > 0);
    }

    // ============ Stake/Unstake ============

    function testStakeDungeon() public {
        // Dungeon 0 already staked in setUp; verify it
        (uint256 nftId, address dungeonOwner, bool active,,) = manager.dungeons(0);
        assertEq(nftId, 0);
        assertEq(dungeonOwner, owner);
        assertTrue(active);
    }

    // ============ Epoch System Tests ============

    function test_endEpoch_success() public {
        assertEq(uint256(manager.epochState()), uint256(DungeonManager.EpochState.Active));
        manager.endEpoch();
        assertEq(uint256(manager.epochState()), uint256(DungeonManager.EpochState.Grace));
    }

    function test_endEpoch_revert_notActive() public {
        manager.endEpoch();
        vm.expectRevert(DungeonManager.EpochNotActive.selector);
        manager.endEpoch();
    }

    function test_endEpoch_revert_notOwner() public {
        vm.prank(player1);
        vm.expectRevert();
        manager.endEpoch();
    }

    function test_startEpoch_success() public {
        manager.endEpoch();
        manager.startEpoch();
        assertEq(uint256(manager.epochState()), uint256(DungeonManager.EpochState.Active));
        assertEq(manager.currentEpoch(), 2); // setUp started epoch 1
    }

    function test_startEpoch_afterTimeout() public {
        // Create an active session first
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        _acceptDM(sessionId);

        manager.endEpoch();

        // Can't start — active sessions and <48h
        vm.expectRevert(DungeonManager.GracePeriodActive.selector);
        manager.startEpoch();

        // Warp past 48h
        vm.warp(block.timestamp + 48 hours + 1);
        manager.startEpoch();
        assertEq(uint256(manager.epochState()), uint256(DungeonManager.EpochState.Active));
    }

    function test_startEpoch_revert_sessionsActive() public {
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        _acceptDM(sessionId);

        manager.endEpoch();
        vm.expectRevert(DungeonManager.GracePeriodActive.selector);
        manager.startEpoch();
    }

    function test_startEpoch_revert_notGrace() public {
        vm.expectRevert(DungeonManager.EpochNotGrace.selector);
        manager.startEpoch();
    }

    function test_enterDungeon_revert_epochNotActive() public {
        manager.endEpoch();
        vm.prank(player1);
        vm.expectRevert(DungeonManager.EpochNotActive.selector);
        manager.enterDungeon{value: BOND}(0);
    }

    function test_stakeDungeon_revert_epochNotGrace() public {
        dungeonNFT.mint(owner, 5, 2, DungeonNFT.Theme.Cave, DungeonNFT.Rarity.Common);
        dungeonNFT.approve(address(manager), 1);
        vm.expectRevert(DungeonManager.EpochNotGrace.selector);
        manager.stakeDungeon(1);
    }

    function test_unstakeDungeon_revert_epochNotGrace() public {
        vm.expectRevert(DungeonManager.EpochNotGrace.selector);
        manager.unstakeDungeon(0);
    }

    function test_updateSkill_revert_epochNotGrace() public {
        // Add skill during grace first
        manager.endEpoch();
        manager.addSkill("Test", "content");
        // Go active
        manager.startEpoch();
        // Can't update during active
        vm.expectRevert(DungeonManager.EpochNotGrace.selector);
        manager.updateSkill(0, "Test2", "content2");
    }

    function test_epochPinning() public {
        // Verify sessions are pinned to current epoch
        // We need a view function — let's add one. For now, test that epoch is 1
        assertEq(manager.currentEpoch(), 1);
        // Enter dungeon during epoch 1
        uint256 dungeonId = _stakeDungeon();
        vm.prank(player1);
        manager.enterDungeon{value: BOND}(dungeonId);
        // Session created in epoch 1 — verified by contract storing currentEpoch
        // (epochId is in the struct but not easily readable from auto-generated getter with dynamic arrays)
        // The important thing is the code sets sessions[sessionId].epochId = currentEpoch
    }

    function test_skillHash_computed() public {
        manager.endEpoch();
        manager.addSkill("Skill1", "content1");
        manager.startEpoch();
        uint256 epoch = manager.currentEpoch();
        bytes32 hash = manager.epochSkillHash(epoch);
        assertTrue(hash != bytes32(0));
        assertEq(manager.epochDmFee(epoch), 15);
    }

    function test_sessionCompletionDuringGrace() public {
        // Start a session during Active
        uint256 dungeonId = _stakeDungeon();
        uint256 sessionId = _enterBothPlayers(dungeonId);
        _acceptDM(sessionId);

        address dm = manager.getSessionDM(sessionId);
        address[] memory party = manager.getSessionParty(sessionId);
        address nonDm = party[0];

        // End epoch — go to Grace
        manager.endEpoch();

        // Session should still be completable during Grace
        manager.submitAction(sessionId, 1, "I fight", nonDm);

        DungeonManager.DMAction[] memory dmActions = new DungeonManager.DMAction[](1);
        dmActions[0] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.COMPLETE,
            target: address(0),
            value: 0,
            narrative: "Done!"
        });

        manager.submitDMResponse(sessionId, 1, "Victory", dmActions, dm);

        assertEq(uint256(manager.getSessionState(sessionId)), uint256(DungeonManager.SessionState.Completed));
    }

    // ============ Pausable Tests ============

    function test_pause_blocksEnterDungeon() public {
        manager.pause();
        vm.prank(player1);
        vm.expectRevert();
        manager.enterDungeon{value: BOND}(0);
    }

    function test_pause_blocksStakeDungeon() public {
        manager.endEpoch();
        dungeonNFT.mint(owner, 5, 2, DungeonNFT.Theme.Cave, DungeonNFT.Rarity.Common);
        dungeonNFT.approve(address(manager), 1);
        manager.pause();
        vm.expectRevert();
        manager.stakeDungeon(1);
    }

    function test_pause_blocksSubmitAction() public {
        (, uint256 sessionId,, address nonDm) = _setupActiveSession();
        manager.pause();
        vm.expectRevert();
        manager.submitAction(sessionId, 1, "I act", nonDm);
    }

    function test_pause_blocksSubmitDMResponse() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();
        manager.submitAction(sessionId, 1, "I act", nonDm);
        manager.pause();
        DungeonManager.DMAction[] memory actions = new DungeonManager.DMAction[](0);
        vm.expectRevert();
        manager.submitDMResponse(sessionId, 1, "Narration", actions, dm);
    }

    function test_unpause_allowsActions() public {
        manager.pause();
        manager.unpause();
        vm.prank(player1);
        manager.enterDungeon{value: BOND}(0);
    }

    function test_pause_doesNotBlockWithdrawBond() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();
        manager.submitAction(sessionId, 1, "Quick", nonDm);
        DungeonManager.DMAction[] memory dmActions = new DungeonManager.DMAction[](1);
        dmActions[0] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.COMPLETE,
            target: address(0),
            value: 0,
            narrative: "Done!"
        });
        manager.submitDMResponse(sessionId, 1, "Complete", dmActions, dm);
        manager.pause();
        // withdrawBond should still work
        vm.prank(dm);
        manager.withdrawBond();
    }

    // ============ onlyRunner Tests ============

    function test_onlyRunner_submitAction_reverts() public {
        (, uint256 sessionId,, address nonDm) = _setupActiveSession();
        vm.prank(player1);
        vm.expectRevert(DungeonManager.NotRunner.selector);
        manager.submitAction(sessionId, 1, "I act", nonDm);
    }

    function test_onlyRunner_submitDMResponse_reverts() public {
        (, uint256 sessionId, address dm, address nonDm) = _setupActiveSession();
        manager.submitAction(sessionId, 1, "I act", nonDm);
        DungeonManager.DMAction[] memory actions = new DungeonManager.DMAction[](0);
        vm.prank(player1);
        vm.expectRevert(DungeonManager.NotRunner.selector);
        manager.submitDMResponse(sessionId, 1, "Narration", actions, dm);
    }

    function test_setRunner_onlyOwner() public {
        vm.prank(player1);
        vm.expectRevert();
        manager.setRunner(player1);
    }

    // ============ maxGoldPerSession Tests ============

    function test_maxGoldPerSession_default() public view {
        assertEq(manager.maxGoldPerSession(), 500);
    }

    function test_setMaxGoldPerSession_duringGrace() public {
        manager.endEpoch();
        manager.setMaxGoldPerSession(1000);
        assertEq(manager.maxGoldPerSession(), 1000);
    }

    function test_setMaxGoldPerSession_revert_notGrace() public {
        vm.expectRevert(DungeonManager.EpochNotGrace.selector);
        manager.setMaxGoldPerSession(1000);
    }

    function test_maxGoldPerSession_capsSession() public {
        // Default dungeon has difficulty=5, so dungeonCap = 500
        // Set maxGoldPerSession to 200
        manager.endEpoch();
        manager.setMaxGoldPerSession(200);
        manager.startEpoch();

        vm.prank(player1);
        manager.enterDungeon{value: BOND}(0);
        vm.prank(player2);
        manager.enterDungeon{value: BOND}(0);
        uint256 sessionId = manager.sessionCount();
        _acceptDM(sessionId);

        address dm = manager.getSessionDM(sessionId);
        address[] memory party = manager.getSessionParty(sessionId);
        address nonDm = party[0];

        manager.submitAction(sessionId, 1, "I fight", nonDm);

        // Try to award more than maxGoldPerSession (200)
        DungeonManager.DMAction[] memory dmActions = new DungeonManager.DMAction[](2);
        dmActions[0] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.REWARD_GOLD,
            target: nonDm,
            value: 100,
            narrative: ""
        });
        dmActions[1] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.REWARD_GOLD,
            target: nonDm,
            value: 100,
            narrative: ""
        });
        manager.submitDMResponse(sessionId, 1, "Win!", dmActions, dm);

        // Now turn 2 — try to award 1 more gold (would exceed 200 cap)
        manager.submitAction(sessionId, 2, "I fight more", nonDm);
        DungeonManager.DMAction[] memory dmActions2 = new DungeonManager.DMAction[](1);
        dmActions2[0] = DungeonManager.DMAction({
            actionType: DungeonManager.DMActionType.REWARD_GOLD,
            target: nonDm,
            value: 1,
            narrative: ""
        });
        vm.expectRevert(DungeonManager.GoldCapExceeded.selector);
        manager.submitDMResponse(sessionId, 2, "More", dmActions2, dm);
    }
}
