// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/utils/Pausable.sol";
import "./Gold.sol";
import "./DungeonNFT.sol";
import "./DungeonTickets.sol";

/**
 * @title DungeonManager
 * @dev Core game engine for on-chain D&D.
 * v2: Random DM selection, entry bonds, DM acceptance, fee distribution,
 *     replay protection, session timeouts.
 */
contract DungeonManager is Ownable, ReentrancyGuard, Pausable {
    // ============ Constants ============

    uint256 public constant BASE_GOLD_RATE = 100;
    uint256 public constant MAX_GOLD_PER_ACTION = 100;
    uint256 public constant MAX_XP_PER_ACTION = 50;
    uint256 public constant ROYALTY_BPS = 500;  // 5%
    uint256 public constant MAX_ACTION_LENGTH = 1000;
    uint256 public constant MAX_NARRATIVE_LENGTH = 2000;
    uint256 public constant MAX_SKILL_LENGTH = 50000;
    uint256 public constant TURN_TIMEOUT = 300;  // 5 min
    uint256 public constant ENTRY_BOND = 0.01 ether;
    uint256 public constant DM_ACCEPT_TIMEOUT = 5 minutes;
    uint256 public constant SESSION_TIMEOUT = 4 hours;
    uint256 public constant DM_FEE_PERCENT = 15;

    // ============ Contracts ============

    Gold public immutable gold;
    DungeonNFT public immutable dungeonNFT;
    DungeonTickets public immutable tickets;

    // ============ Enums ============

    enum SessionState { Waiting, WaitingDM, Active, Completed, Failed, Cancelled, TimedOut }
    enum DMActionType { NARRATE, REWARD_GOLD, REWARD_XP, DAMAGE, KILL_PLAYER, COMPLETE, FAIL }
    enum EpochState { Active, Grace }

    // ============ Structs ============

    struct Skill {
        string name;
        string content;
        uint256 updatedAt;
        uint256 lockedUntil;
    }

    struct Dungeon {
        uint256 nftId;
        address owner;
        bool active;
        uint256 lootPool;
        uint256 currentSessionId;
    }

    struct DMAction {
        DMActionType actionType;
        address target;
        uint256 value;
        string narrative;
    }

    struct Session {
        uint256 dungeonId;
        address dm;
        address[] party;          // non-DM players
        address[] allPlayers;     // everyone who entered (for bond tracking)
        SessionState state;
        uint256 turnNumber;
        address currentActor;
        uint256 turnDeadline;
        uint256 goldPool;
        uint256 maxGold;
        uint256 actedThisTurn;
        uint256 dmAcceptDeadline;
        uint256 lastActivityTs;
        uint64 dmEpoch;
        uint256 epochId;
    }

    // ============ State ============

    Skill[] public skills;
    uint256 public activeSessionCount;

    // ============ Runner & Caps ============
    address public runner;
    uint256 public maxGoldPerSession = 500;

    // ============ Epoch State ============
    uint256 public currentEpoch;
    EpochState public epochState;
    uint256 public graceStartTime;
    uint256 public constant MAX_GRACE_PERIOD = 48 hours;

    mapping(uint256 => bytes32) public epochSkillHash;
    mapping(uint256 => uint256) public epochDmFee;

    mapping(uint256 => Dungeon) public dungeons;
    uint256 public dungeonCount;

    mapping(uint256 => Session) public sessions;
    mapping(uint256 => mapping(address => uint256)) public sessionPlayerGold;
    mapping(uint256 => mapping(address => bool)) public sessionPlayerAlive;
    uint256 public sessionCount;

    // Replay protection: sessionId => turnNumber => submitted
    mapping(uint256 => mapping(uint256 => bool)) public actionSubmitted;

    // Bonds
    mapping(uint256 => mapping(address => uint256)) public bondOf;
    mapping(address => uint256) public withdrawableBonds;

    // Agents
    mapping(address => bool) public registeredAgents;
    mapping(address => uint256) public xp;
    mapping(address => uint256) public totalGoldEarned;
    address[] public allAgents;

    // Royalties
    mapping(address => uint256) public pendingRoyalties;

    // ============ Events ============

    event RunnerUpdated(address indexed runner);
    event MaxGoldPerSessionUpdated(uint256 newMax);

    event SkillAdded(uint256 indexed skillId, string name);
    event SkillUpdated(uint256 indexed skillId, string name);
    event SkillRemoved(uint256 indexed skillId);

    event DungeonActivated(uint256 indexed dungeonId, uint256 nftId, address owner);
    event DungeonDeactivated(uint256 indexed dungeonId);
    event LootPoolUpdated(uint256 indexed dungeonId, uint256 newTotal);

    event AgentRegistered(address indexed agent);
    event AgentUnregistered(address indexed agent);

    event PlayerEntered(uint256 indexed sessionId, address indexed agent);
    event GameStarted(uint256 indexed sessionId, uint256 indexed dungeonId, address dm, address[] party);
    event ActionSubmitted(uint256 indexed sessionId, address indexed agent, uint256 turn, string action);
    event DMResponse(uint256 indexed sessionId, uint256 turn, string narrative);
    event GoldAwarded(uint256 indexed sessionId, address indexed player, uint256 amount);
    event XPAwarded(uint256 indexed sessionId, address indexed player, uint256 amount);
    event PlayerDied(uint256 indexed sessionId, address indexed agent, uint256 goldToLootPool);
    event PlayerFled(uint256 indexed sessionId, address indexed agent, uint256 goldKept, uint256 royaltyPaid);
    event DungeonCompleted(uint256 indexed sessionId, uint256 totalGoldMinted, uint256 royaltyPaid, string recap);
    event DungeonFailed(uint256 indexed sessionId, uint256 goldToLootPool, string recap);
    event TurnAdvanced(uint256 indexed sessionId, uint256 newTurn, address nextActor);
    event TurnTimeout(uint256 indexed sessionId, address indexed agent);
    event RoyaltyClaimed(address indexed owner, uint256 amount);
    event DmSelected(uint256 indexed sessionId, address indexed dm, uint64 epoch);
    event DmAccepted(uint256 indexed sessionId, address indexed dm);
    event DmRerolled(uint256 indexed sessionId);
    event BondForfeited(uint256 indexed sessionId, address indexed player, uint256 amount);
    event SessionTimedOut(uint256 indexed sessionId);
    event SessionCancelled(uint256 indexed sessionId);
    event BondWithdrawn(address indexed player, uint256 amount);
    event EpochEnded(uint256 indexed epoch);
    event EpochStarted(uint256 indexed epoch, bytes32 skillHash, uint256 dmFee);

    // ============ Errors ============

    error NotRegisteredAgent();
    error NotYourTurn();
    error SessionNotActive();
    error SessionNotWaiting();
    error DungeonNotActive();
    error DungeonHasActiveSession();
    error NotDungeonOwner();
    error NotSessionDM();
    error PartyFull();
    error AlreadyInParty();
    error PlayerNotAlive();
    error GoldCapExceeded();
    error XPCapExceeded();
    error ActionTooLong();
    error NarrativeTooLong();
    error SkillContentTooLong();
    error SkillLocked();
    error TimeoutNotReached();
    error InvalidTarget();
    error InvalidSkillId();
    error DungeonNotOwned();
    error InsufficientTickets();
    error NotConfigured();
    error InvalidDungeonId();
    error InsufficientBond();
    error WrongTurn();
    error AlreadySubmitted();
    error NoActionYet();
    error NotWaitingDM();
    error NotSelectedDM();
    error StaleEpoch();
    error DeadlineNotPassed();
    error NothingToWithdraw();
    error TransferFailed();
    error NotTimedOut();
    error SessionNotTimeoutable();
    error EpochNotActive();
    error EpochNotGrace();
    error GracePeriodActive();
    error NotRunner();

    // ============ Constructor ============

    constructor(
        address _gold,
        address _dungeonNFT,
        address _tickets
    ) Ownable(msg.sender) {
        gold = Gold(_gold);
        dungeonNFT = DungeonNFT(_dungeonNFT);
        tickets = DungeonTickets(_tickets);
        epochState = EpochState.Grace;
        graceStartTime = block.timestamp;
    }

    // ============ Configuration Check ============

    function isConfigured() public view returns (bool) {
        return gold.minter() == address(this) && tickets.burner() == address(this);
    }

    modifier onlyConfigured() {
        if (!isConfigured()) revert NotConfigured();
        _;
    }

    modifier onlyRunner() {
        if (msg.sender != runner) revert NotRunner();
        _;
    }

    function setRunner(address _runner) external onlyOwner {
        runner = _runner;
        emit RunnerUpdated(_runner);
    }

    function setMaxGoldPerSession(uint256 _max) external onlyOwner {
        if (epochState != EpochState.Grace) revert EpochNotGrace();
        maxGoldPerSession = _max;
        emit MaxGoldPerSessionUpdated(_max);
    }

    function pause() external onlyOwner { _pause(); }
    function unpause() external onlyOwner { _unpause(); }

    // ============ Epoch Management ============

    function endEpoch() external onlyOwner {
        if (epochState != EpochState.Active) revert EpochNotActive();
        epochState = EpochState.Grace;
        graceStartTime = block.timestamp;
        emit EpochEnded(currentEpoch);
    }

    function startEpoch() external onlyOwner {
        if (epochState != EpochState.Grace) revert EpochNotGrace();
        if (activeSessionCount > 0 && block.timestamp <= graceStartTime + MAX_GRACE_PERIOD) {
            revert GracePeriodActive();
        }
        currentEpoch++;
        epochState = EpochState.Active;
        epochSkillHash[currentEpoch] = _computeSkillHash();
        epochDmFee[currentEpoch] = DM_FEE_PERCENT;
        emit EpochStarted(currentEpoch, epochSkillHash[currentEpoch], DM_FEE_PERCENT);
    }

    function _computeSkillHash() internal view returns (bytes32) {
        bytes memory packed;
        for (uint256 i = 0; i < skills.length; i++) {
            packed = abi.encodePacked(packed, skills[i].content);
        }
        return keccak256(packed);
    }

    // ============ Skill Management ============

    function addSkill(string calldata name, string calldata content) external onlyOwner returns (uint256 skillId) {
        if (bytes(content).length > MAX_SKILL_LENGTH) revert SkillContentTooLong();
        skillId = skills.length;
        skills.push(Skill({ name: name, content: content, updatedAt: block.timestamp, lockedUntil: 0 }));
        emit SkillAdded(skillId, name);
    }

    function updateSkill(uint256 skillId, string calldata name, string calldata content) external onlyOwner {
        if (skillId >= skills.length) revert InvalidSkillId();
        if (bytes(content).length > MAX_SKILL_LENGTH) revert SkillContentTooLong();
        if (epochState != EpochState.Grace) revert EpochNotGrace();
        Skill storage skill = skills[skillId];
        skill.name = name;
        skill.content = content;
        skill.updatedAt = block.timestamp;
        emit SkillUpdated(skillId, name);
    }

    function getSkill(uint256 skillId) external view returns (Skill memory) {
        if (skillId >= skills.length) revert InvalidSkillId();
        return skills[skillId];
    }

    function getSkillCount() external view returns (uint256) {
        return skills.length;
    }

    // ============ Agent Management ============

    function registerAgent(address agent) external onlyOwner {
        if (!registeredAgents[agent]) {
            registeredAgents[agent] = true;
            allAgents.push(agent);
            emit AgentRegistered(agent);
        }
    }

    function unregisterAgent(address agent) external onlyOwner {
        registeredAgents[agent] = false;
        emit AgentUnregistered(agent);
    }

    function batchRegisterAgents(address[] calldata agents) external onlyOwner {
        for (uint256 i = 0; i < agents.length; i++) {
            if (!registeredAgents[agents[i]]) {
                registeredAgents[agents[i]] = true;
                allAgents.push(agents[i]);
                emit AgentRegistered(agents[i]);
            }
        }
    }

    // ============ Dungeon Staking ============

    function stakeDungeon(uint256 nftId) external nonReentrant whenNotPaused returns (uint256 dungeonId) {
        if (epochState != EpochState.Grace) revert EpochNotGrace();
        dungeonNFT.transferFrom(msg.sender, address(this), nftId);
        dungeonId = dungeonCount++;
        dungeons[dungeonId] = Dungeon({
            nftId: nftId, owner: msg.sender, active: true, lootPool: 0, currentSessionId: 0
        });
        emit DungeonActivated(dungeonId, nftId, msg.sender);
    }

    function unstakeDungeon(uint256 dungeonId) external nonReentrant {
        if (epochState != EpochState.Grace) revert EpochNotGrace();
        if (dungeonId >= dungeonCount) revert InvalidDungeonId();
        Dungeon storage dungeon = dungeons[dungeonId];
        if (dungeon.owner != msg.sender) revert NotDungeonOwner();
        if (!dungeon.active) revert DungeonNotActive();
        if (dungeon.currentSessionId != 0) {
            Session storage session = sessions[dungeon.currentSessionId];
            if (session.state == SessionState.Active || session.state == SessionState.Waiting || session.state == SessionState.WaitingDM) {
                revert DungeonHasActiveSession();
            }
        }
        dungeon.active = false;
        dungeonNFT.transferFrom(address(this), msg.sender, dungeon.nftId);
        emit DungeonDeactivated(dungeonId);
    }

    // ============ Session Management ============

    function enterDungeon(uint256 dungeonId) external payable nonReentrant onlyConfigured whenNotPaused {
        if (epochState != EpochState.Active) revert EpochNotActive();
        if (!registeredAgents[msg.sender]) revert NotRegisteredAgent();
        if (dungeonId >= dungeonCount) revert InvalidDungeonId();
        if (msg.value < ENTRY_BOND) revert InsufficientBond();

        Dungeon storage dungeon = dungeons[dungeonId];
        if (!dungeon.active) revert DungeonNotActive();

        // Burn 1 ticket
        if (tickets.balanceOf(msg.sender, 0) == 0) revert InsufficientTickets();
        tickets.burnTicket(msg.sender, 1);

        DungeonNFT.DungeonTraits memory traits = dungeonNFT.getTraits(dungeon.nftId);

        // Check for existing waiting session
        if (dungeon.currentSessionId != 0) {
            Session storage session = sessions[dungeon.currentSessionId];
            if (session.state == SessionState.Waiting) {
                // allPlayers includes everyone; party will be set after DM selection
                if (session.allPlayers.length >= traits.partySize) revert PartyFull();
                for (uint256 i = 0; i < session.allPlayers.length; i++) {
                    if (session.allPlayers[i] == msg.sender) revert AlreadyInParty();
                }
                session.allPlayers.push(msg.sender);
                bondOf[dungeon.currentSessionId][msg.sender] = msg.value;
                sessionPlayerAlive[dungeon.currentSessionId][msg.sender] = true;
                emit PlayerEntered(dungeon.currentSessionId, msg.sender);

                // If party full, select DM
                if (session.allPlayers.length == traits.partySize) {
                    _selectDM(dungeon.currentSessionId);
                }
                return;
            }
        }

        // Create new session
        uint256 sessionId = ++sessionCount;
        dungeon.currentSessionId = sessionId;

        sessions[sessionId].dungeonId = dungeonId;
        sessions[sessionId].state = SessionState.Waiting;
        uint256 dungeonCap = uint256(traits.difficulty) * BASE_GOLD_RATE;
        sessions[sessionId].maxGold = dungeonCap < maxGoldPerSession ? dungeonCap : maxGoldPerSession;
        sessions[sessionId].epochId = currentEpoch;
        sessions[sessionId].allPlayers.push(msg.sender);

        bondOf[sessionId][msg.sender] = msg.value;
        sessionPlayerAlive[sessionId][msg.sender] = true;
        activeSessionCount++;

        emit PlayerEntered(sessionId, msg.sender);

        // Auto-select DM if single-player (partySize == 1, though min is typically 2)
        if (traits.partySize == 1) {
            _selectDM(sessionId);
        }
    }

    // ============ DM Selection & Acceptance ============

    function _selectDM(uint256 sessionId) internal {
        Session storage s = sessions[sessionId];

        uint256 seed = uint256(keccak256(abi.encodePacked(
            block.prevrandao,
            blockhash(block.number - 1),
            sessionId,
            s.allPlayers
        )));
        uint256 dmIndex = seed % s.allPlayers.length;

        s.dm = s.allPlayers[dmIndex];

        // Build party (everyone except DM)
        delete s.party;
        for (uint256 i = 0; i < s.allPlayers.length; i++) {
            if (i != dmIndex) {
                s.party.push(s.allPlayers[i]);
            }
        }

        s.dmAcceptDeadline = block.timestamp + DM_ACCEPT_TIMEOUT;
        s.dmEpoch++;
        s.state = SessionState.WaitingDM;
        s.lastActivityTs = block.timestamp;

        emit DmSelected(sessionId, s.dm, s.dmEpoch);
    }

    function acceptDM(uint256 sessionId, uint64 epoch, address dm) external onlyRunner {
        Session storage s = sessions[sessionId];
        if (s.state != SessionState.WaitingDM) revert NotWaitingDM();
        if (dm != s.dm) revert NotSelectedDM();
        if (epoch != s.dmEpoch) revert StaleEpoch();
        if (block.timestamp > s.dmAcceptDeadline) revert DeadlineNotPassed();

        s.state = SessionState.Active;
        s.lastActivityTs = block.timestamp;
        s.turnNumber = 1;
        // First actor is the first player (not DM). Players act, then DM responds.
        s.currentActor = s.party[0];
        s.turnDeadline = block.timestamp + TURN_TIMEOUT;

        emit DmAccepted(sessionId, dm);
        emit GameStarted(sessionId, s.dungeonId, s.dm, s.party);
    }

    function rerollDM(uint256 sessionId) external {
        Session storage s = sessions[sessionId];
        if (s.state != SessionState.WaitingDM) revert NotWaitingDM();
        if (block.timestamp <= s.dmAcceptDeadline) revert TimeoutNotReached();

        // Forfeit old DM's bond
        _forfeitBond(s.dm, sessionId);

        // Remove old DM from allPlayers
        _removeFromAllPlayers(s, s.dm);

        s.lastActivityTs = block.timestamp;

        if (s.allPlayers.length >= 2) {
            // Re-select from remaining players
            _selectDM(sessionId);
        } else {
            // Not enough players left
            s.state = SessionState.Cancelled;
            activeSessionCount--;
            // Release remaining bonds
            for (uint256 i = 0; i < s.allPlayers.length; i++) {
                _releaseBond(s.allPlayers[i], sessionId);
            }
            emit SessionCancelled(sessionId);
        }

        emit DmRerolled(sessionId);
    }

    function _removeFromAllPlayers(Session storage s, address player) internal {
        for (uint256 i = 0; i < s.allPlayers.length; i++) {
            if (s.allPlayers[i] == player) {
                s.allPlayers[i] = s.allPlayers[s.allPlayers.length - 1];
                s.allPlayers.pop();
                return;
            }
        }
    }

    // ============ Bond Management ============

    function _forfeitBond(address player, uint256 sessionId) internal {
        uint256 bond = bondOf[sessionId][player];
        if (bond > 0) {
            bondOf[sessionId][player] = 0;
            dungeons[sessions[sessionId].dungeonId].lootPool += bond;
            emit BondForfeited(sessionId, player, bond);
        }
    }

    function _releaseBond(address player, uint256 sessionId) internal {
        uint256 bond = bondOf[sessionId][player];
        if (bond > 0) {
            bondOf[sessionId][player] = 0;
            withdrawableBonds[player] += bond;
        }
    }

    function withdrawBond() external nonReentrant {
        uint256 amount = withdrawableBonds[msg.sender];
        if (amount == 0) revert NothingToWithdraw();
        withdrawableBonds[msg.sender] = 0;
        (bool success, ) = payable(msg.sender).call{value: amount}("");
        if (!success) revert TransferFailed();
        emit BondWithdrawn(msg.sender, amount);
    }

    // ============ Game Actions (with Replay Protection) ============

    function submitAction(uint256 sessionId, uint256 turnIndex, string calldata action, address player) external onlyRunner whenNotPaused {
        if (bytes(action).length > MAX_ACTION_LENGTH) revert ActionTooLong();

        Session storage session = sessions[sessionId];
        if (session.state != SessionState.Active) revert SessionNotActive();
        if (player != session.currentActor) revert NotYourTurn();
        if (!sessionPlayerAlive[sessionId][player]) revert PlayerNotAlive();
        if (turnIndex != session.turnNumber) revert WrongTurn();

        // Set flag for DM's NoActionYet check (idempotent, first player sets it)
        actionSubmitted[sessionId][turnIndex] = true;
        session.lastActivityTs = block.timestamp;

        emit ActionSubmitted(sessionId, player, session.turnNumber, action);

        // Mark player as acted
        uint256 playerIndex = _getPlayerIndex(session, player);
        session.actedThisTurn |= (1 << playerIndex);

        _advanceToNextActor(sessionId);
    }

    function submitDMResponse(
        uint256 sessionId,
        uint256 turnIndex,
        string calldata narrative,
        DMAction[] calldata actions,
        address dm
    ) external onlyRunner whenNotPaused {
        if (bytes(narrative).length > MAX_NARRATIVE_LENGTH) revert NarrativeTooLong();

        Session storage session = sessions[sessionId];
        if (session.state != SessionState.Active) revert SessionNotActive();
        if (dm != session.dm) revert NotSessionDM();
        if (dm != session.currentActor) revert NotYourTurn();
        if (turnIndex != session.turnNumber) revert WrongTurn();
        if (!actionSubmitted[sessionId][turnIndex]) revert NoActionYet();

        session.lastActivityTs = block.timestamp;

        // Process DM actions
        for (uint256 i = 0; i < actions.length; i++) {
            _processDMAction(sessionId, actions[i]);
        }

        emit DMResponse(sessionId, session.turnNumber, narrative);

        if (session.state != SessionState.Active) return;

        // Advance turn
        session.turnNumber++;
        session.actedThisTurn = 0;
        _advanceToNextActor(sessionId);
    }

    function _processDMAction(uint256 sessionId, DMAction calldata action) internal {
        Session storage session = sessions[sessionId];
        Dungeon storage dungeon = dungeons[session.dungeonId];

        if (action.actionType == DMActionType.REWARD_GOLD) {
            if (action.value > MAX_GOLD_PER_ACTION) revert GoldCapExceeded();
            if (session.goldPool + action.value > session.maxGold) revert GoldCapExceeded();
            if (!sessionPlayerAlive[sessionId][action.target]) revert InvalidTarget();
            sessionPlayerGold[sessionId][action.target] += action.value;
            session.goldPool += action.value;
            emit GoldAwarded(sessionId, action.target, action.value);
        } else if (action.actionType == DMActionType.REWARD_XP) {
            if (action.value > MAX_XP_PER_ACTION) revert XPCapExceeded();
            if (!sessionPlayerAlive[sessionId][action.target]) revert InvalidTarget();
            xp[action.target] += action.value;
            emit XPAwarded(sessionId, action.target, action.value);
        } else if (action.actionType == DMActionType.KILL_PLAYER) {
            if (!sessionPlayerAlive[sessionId][action.target]) revert InvalidTarget();
            if (action.target == session.dm) revert InvalidTarget();
            sessionPlayerAlive[sessionId][action.target] = false;
            uint256 goldLost = sessionPlayerGold[sessionId][action.target];
            sessionPlayerGold[sessionId][action.target] = 0;
            dungeon.lootPool += goldLost;
            emit PlayerDied(sessionId, action.target, goldLost);
            emit LootPoolUpdated(session.dungeonId, dungeon.lootPool);
            if (_allPlayersDead(sessionId)) {
                _failSession(sessionId, action.narrative);
            }
        } else if (action.actionType == DMActionType.COMPLETE) {
            _completeSession(sessionId, action.narrative);
        } else if (action.actionType == DMActionType.FAIL) {
            _failSession(sessionId, action.narrative);
        }
    }

    function _advanceToNextActor(uint256 sessionId) internal {
        Session storage session = sessions[sessionId];

        if (session.currentActor == session.dm) {
            for (uint256 i = 0; i < session.party.length; i++) {
                if (sessionPlayerAlive[sessionId][session.party[i]]) {
                    uint256 allIdx = _getAllPlayerIndex(session, session.party[i]);
                    uint256 playerBit = 1 << allIdx;
                    if ((session.actedThisTurn & playerBit) == 0) {
                        session.currentActor = session.party[i];
                        session.turnDeadline = block.timestamp + TURN_TIMEOUT;
                        emit TurnAdvanced(sessionId, session.turnNumber, session.currentActor);
                        return;
                    }
                }
            }
            session.currentActor = session.dm;
            session.turnDeadline = block.timestamp + TURN_TIMEOUT;
            emit TurnAdvanced(sessionId, session.turnNumber, session.dm);
        } else {
            uint256 startPartyIdx = _getPartyIndex(session, session.currentActor);
            bool found = false;
            for (uint256 offset = 1; offset < session.party.length; offset++) {
                uint256 i = (startPartyIdx + offset) % session.party.length;
                if (sessionPlayerAlive[sessionId][session.party[i]]) {
                    uint256 allIdx = _getAllPlayerIndex(session, session.party[i]);
                    uint256 playerBit = 1 << allIdx;
                    if ((session.actedThisTurn & playerBit) == 0) {
                        session.currentActor = session.party[i];
                        session.turnDeadline = block.timestamp + TURN_TIMEOUT;
                        found = true;
                        emit TurnAdvanced(sessionId, session.turnNumber, session.currentActor);
                        break;
                    }
                }
            }
            if (!found) {
                session.currentActor = session.dm;
                session.turnDeadline = block.timestamp + TURN_TIMEOUT;
                emit TurnAdvanced(sessionId, session.turnNumber, session.dm);
            }
        }
    }

    function _getPlayerIndex(Session storage session, address player) internal view returns (uint256) {
        return _getAllPlayerIndex(session, player);
    }

    function _getAllPlayerIndex(Session storage session, address player) internal view returns (uint256) {
        for (uint256 i = 0; i < session.allPlayers.length; i++) {
            if (session.allPlayers[i] == player) return i;
        }
        return 0;
    }

    function _getPartyIndex(Session storage session, address player) internal view returns (uint256) {
        for (uint256 i = 0; i < session.party.length; i++) {
            if (session.party[i] == player) return i;
        }
        return 0;
    }

    function _allPlayersDead(uint256 sessionId) internal view returns (bool) {
        Session storage session = sessions[sessionId];
        for (uint256 i = 0; i < session.party.length; i++) {
            if (sessionPlayerAlive[sessionId][session.party[i]]) return false;
        }
        return true;
    }

    // ============ Session Resolution ============

    function flee(uint256 sessionId, address agent) external nonReentrant onlyConfigured onlyRunner {
        Session storage session = sessions[sessionId];
        if (session.state != SessionState.Active) revert SessionNotActive();
        if (!sessionPlayerAlive[sessionId][agent]) revert PlayerNotAlive();
        if (agent == session.dm) revert InvalidTarget();

        uint256 playerGold = sessionPlayerGold[sessionId][agent];
        sessionPlayerGold[sessionId][agent] = 0;
        sessionPlayerAlive[sessionId][agent] = false;

        uint256 royalty = (playerGold * ROYALTY_BPS) / 10000;
        uint256 goldToPlayer = playerGold - royalty;

        Dungeon storage dungeon = dungeons[session.dungeonId];
        pendingRoyalties[dungeon.owner] += royalty;

        if (goldToPlayer > 0) {
            gold.mint(agent, goldToPlayer);
            totalGoldEarned[agent] += goldToPlayer;
        }

        // Release bond on flee
        _releaseBond(agent, sessionId);

        emit PlayerFled(sessionId, agent, goldToPlayer, royalty);

        if (_allPlayersDead(sessionId)) {
            _failSession(sessionId, "All adventurers fled or perished.");
        }
    }

    function _completeSession(uint256 sessionId, string memory recap) internal {
        Session storage session = sessions[sessionId];
        // Idempotent: if already completed/failed/cancelled, skip
        if (session.state != SessionState.Active && session.state != SessionState.WaitingDM) {
            return;
        }

        session.state = SessionState.Completed;
        activeSessionCount--;

        (uint256 totalMinted, uint256 royalty) = _distributeGold(sessionId);
        _releaseAllBonds(sessionId);

        emit DungeonCompleted(sessionId, totalMinted, royalty, recap);
    }

    function _distributeGold(uint256 sessionId) internal returns (uint256 totalMinted, uint256 royalty) {
        Session storage session = sessions[sessionId];
        uint256 totalGold = session.goldPool;
        uint256 dmFee = (totalGold * DM_FEE_PERCENT) / 100;
        royalty = (totalGold * ROYALTY_BPS) / 10000;
        uint256 playersShare = totalGold - dmFee - royalty;

        if (dmFee > 0) {
            gold.mint(session.dm, dmFee);
            totalGoldEarned[session.dm] += dmFee;
        }

        pendingRoyalties[dungeons[session.dungeonId].owner] += royalty;

        totalMinted = dmFee + _distributePlayerShares(sessionId, playersShare);
    }

    function _distributePlayerShares(uint256 sessionId, uint256 playersShare) internal returns (uint256 minted) {
        Session storage session = sessions[sessionId];
        uint256 totalPlayerGold = 0;
        for (uint256 i = 0; i < session.party.length; i++) {
            if (sessionPlayerAlive[sessionId][session.party[i]]) {
                totalPlayerGold += sessionPlayerGold[sessionId][session.party[i]];
            }
        }
        if (totalPlayerGold == 0) return 0;

        for (uint256 i = 0; i < session.party.length; i++) {
            address player = session.party[i];
            if (!sessionPlayerAlive[sessionId][player]) continue;
            uint256 pGold = sessionPlayerGold[sessionId][player];
            if (pGold == 0) continue;
            uint256 share = (pGold * playersShare) / totalPlayerGold;
            if (share > 0) {
                gold.mint(player, share);
                totalGoldEarned[player] += share;
                minted += share;
            }
        }
    }

    function _releaseAllBonds(uint256 sessionId) internal {
        Session storage session = sessions[sessionId];
        _releaseBond(session.dm, sessionId);
        for (uint256 i = 0; i < session.allPlayers.length; i++) {
            _releaseBond(session.allPlayers[i], sessionId);
        }
    }

    function _failSession(uint256 sessionId, string memory recap) internal {
        Session storage session = sessions[sessionId];
        // Idempotent: if already failed/completed/cancelled, skip
        if (session.state != SessionState.Active && session.state != SessionState.WaitingDM) {
            return;
        }
        Dungeon storage dungeon = dungeons[session.dungeonId];

        session.state = SessionState.Failed;
        activeSessionCount--;

        uint256 goldToLootPool = 0;
        for (uint256 i = 0; i < session.allPlayers.length; i++) {
            address player = session.allPlayers[i];
            uint256 playerGold = sessionPlayerGold[sessionId][player];
            goldToLootPool += playerGold;
            sessionPlayerGold[sessionId][player] = 0;
        }

        dungeon.lootPool += goldToLootPool;

        // Forfeit all bonds on failure (bonds go to dungeon loot pool)
        _forfeitAllBonds(sessionId);

        emit DungeonFailed(sessionId, goldToLootPool, recap);
        emit LootPoolUpdated(session.dungeonId, dungeon.lootPool);
    }

    function _forfeitAllBonds(uint256 sessionId) internal {
        Session storage session = sessions[sessionId];
        _forfeitBond(session.dm, sessionId);
        for (uint256 i = 0; i < session.allPlayers.length; i++) {
            _forfeitBond(session.allPlayers[i], sessionId);
        }
    }

    // ============ Session Timeout ============

    function timeoutSession(uint256 sessionId) external {
        Session storage s = sessions[sessionId];
        if (s.state != SessionState.WaitingDM && s.state != SessionState.Active) revert SessionNotTimeoutable();
        if (block.timestamp <= s.lastActivityTs + SESSION_TIMEOUT) revert NotTimedOut();

        s.state = SessionState.TimedOut;
        activeSessionCount--;

        // Release all bonds
        if (bondOf[sessionId][s.dm] > 0) {
            _releaseBond(s.dm, sessionId);
        }
        for (uint256 i = 0; i < s.allPlayers.length; i++) {
            if (bondOf[sessionId][s.allPlayers[i]] > 0) {
                _releaseBond(s.allPlayers[i], sessionId);
            }
        }

        emit SessionTimedOut(sessionId);
    }

    // ============ Turn Timeouts ============

    function timeoutAdvance(uint256 sessionId) external {
        Session storage session = sessions[sessionId];
        if (session.state != SessionState.Active) revert SessionNotActive();
        if (block.timestamp <= session.turnDeadline) revert TimeoutNotReached();

        address timedOutActor = session.currentActor;
        emit TurnTimeout(sessionId, timedOutActor);

        if (timedOutActor == session.dm) {
            _failSession(sessionId, "The Dungeon Master abandoned the adventure.");
            return;
        }

        uint256 playerIndex = _getPlayerIndex(session, timedOutActor);
        session.actedThisTurn |= (1 << playerIndex);
        session.lastActivityTs = block.timestamp;
        _advanceToNextActor(sessionId);
    }

    // ============ Royalties ============

    function claimRoyalties() external nonReentrant onlyConfigured {
        uint256 amount = pendingRoyalties[msg.sender];
        if (amount > 0) {
            pendingRoyalties[msg.sender] = 0;
            gold.mint(msg.sender, amount);
            emit RoyaltyClaimed(msg.sender, amount);
        }
    }

    // ============ Loot Pool ============

    function awardFromLootPool(uint256 sessionId, address player, uint256 amount) external {
        Session storage session = sessions[sessionId];
        if (session.state != SessionState.Active) revert SessionNotActive();
        if (msg.sender != session.dm) revert NotSessionDM();
        if (!sessionPlayerAlive[sessionId][player]) revert InvalidTarget();
        if (amount > MAX_GOLD_PER_ACTION) revert GoldCapExceeded();
        if (session.goldPool + amount > session.maxGold) revert GoldCapExceeded();

        Dungeon storage dungeon = dungeons[session.dungeonId];
        require(amount <= dungeon.lootPool, "Insufficient loot pool");
        dungeon.lootPool -= amount;
        sessionPlayerGold[sessionId][player] += amount;
        session.goldPool += amount;
        emit GoldAwarded(sessionId, player, amount);
        emit LootPoolUpdated(session.dungeonId, dungeon.lootPool);
    }

    // ============ View Functions ============

    function getSessionParty(uint256 sessionId) external view returns (address[] memory) {
        return sessions[sessionId].party;
    }

    function getSessionAllPlayers(uint256 sessionId) external view returns (address[] memory) {
        return sessions[sessionId].allPlayers;
    }

    function getSessionState(uint256 sessionId) external view returns (SessionState) {
        return sessions[sessionId].state;
    }

    function getSessionDM(uint256 sessionId) external view returns (address) {
        return sessions[sessionId].dm;
    }

    function getSessionTurn(uint256 sessionId) external view returns (uint256) {
        return sessions[sessionId].turnNumber;
    }

    function getSessionDmEpoch(uint256 sessionId) external view returns (uint64) {
        return sessions[sessionId].dmEpoch;
    }

    // ============ Leaderboard Views ============

    function getTopByXP(uint256 count) external view returns (address[] memory, uint256[] memory) {
        uint256 total = allAgents.length;
        if (count > total) count = total;
        address[] memory topAgents = new address[](count);
        uint256[] memory topXP = new uint256[](count);
        bool[] memory used = new bool[](total);
        for (uint256 i = 0; i < count; i++) {
            uint256 maxXP = 0;
            uint256 maxIndex = 0;
            for (uint256 j = 0; j < total; j++) {
                if (!used[j] && xp[allAgents[j]] >= maxXP) {
                    maxXP = xp[allAgents[j]];
                    maxIndex = j;
                }
            }
            used[maxIndex] = true;
            topAgents[i] = allAgents[maxIndex];
            topXP[i] = maxXP;
        }
        return (topAgents, topXP);
    }

    function getTopByGold(uint256 count) external view returns (address[] memory, uint256[] memory) {
        uint256 total = allAgents.length;
        if (count > total) count = total;
        address[] memory topAgents = new address[](count);
        uint256[] memory topGold = new uint256[](count);
        bool[] memory used = new bool[](total);
        for (uint256 i = 0; i < count; i++) {
            uint256 maxGold = 0;
            uint256 maxIndex = 0;
            for (uint256 j = 0; j < total; j++) {
                if (!used[j] && totalGoldEarned[allAgents[j]] >= maxGold) {
                    maxGold = totalGoldEarned[allAgents[j]];
                    maxIndex = j;
                }
            }
            used[maxIndex] = true;
            topAgents[i] = allAgents[maxIndex];
            topGold[i] = maxGold;
        }
        return (topAgents, topGold);
    }

    function getAgentStats(address agent) external view returns (
        uint256 agentXP, uint256 agentGold, bool isRegistered
    ) {
        return (xp[agent], totalGoldEarned[agent], registeredAgents[agent]);
    }

    // ============ Receive ============

    receive() external payable {}
}
