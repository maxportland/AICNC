#!/bin/bash
#
# Update the cam_ir submodule to the latest commit on its branch and reinstall it into
# this config's venv.
#
#   ./update_cam_ir.sh                  pull and reinstall if there are new commits
#   ./update_cam_ir.sh --force-install  reinstall even if nothing was pulled
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAM_IR_DIR="$CONFIG_DIR/libs/cam_ir"
PIP="$CONFIG_DIR/venv/bin/pip"

echo -e "${BLUE}=== cam_ir Update Script ===${NC}"

if ! git -C "$CAM_IR_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    echo -e "${RED}Error: $CAM_IR_DIR is not a git checkout. Run: git submodule update --init${NC}"
    exit 1
fi
if [ ! -x "$PIP" ]; then
    echo -e "${RED}Error: $PIP not found. Create the venv first (see README).${NC}"
    exit 1
fi

cd "$CAM_IR_DIR"

# A fresh submodule checkout is on a detached HEAD; follow main in that case
BRANCH=$(git branch --show-current)
if [ -z "$BRANCH" ]; then
    BRANCH=main
    git checkout -q "$BRANCH"
fi
echo -e "${BLUE}Branch: $BRANCH${NC}"

git fetch origin "$BRANCH"
LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "origin/$BRANCH")

UPDATED=false
if [ "$LOCAL_COMMIT" = "$REMOTE_COMMIT" ]; then
    echo -e "${GREEN}✓ Already up to date.${NC}"
else
    STASHED=false
    if ! git diff-index --quiet HEAD --; then
        echo -e "${YELLOW}Stashing local changes before pulling...${NC}"
        git stash push -m "update_cam_ir.sh $(date +%Y-%m-%d_%H:%M:%S)"
        STASHED=true
    fi

    # Fast-forward only: never create merge commits in the submodule
    if git pull --ff-only origin "$BRANCH"; then
        echo -e "${GREEN}✓ Updated $LOCAL_COMMIT -> $REMOTE_COMMIT${NC}"
        UPDATED=true
    else
        echo -e "${RED}Error: can't fast-forward (local commits?). Resolve by hand in $CAM_IR_DIR${NC}"
    fi

    if [ "$STASHED" = true ]; then
        echo -e "${BLUE}Restoring stashed changes...${NC}"
        git stash pop || echo -e "${YELLOW}Stash didn't apply cleanly; it is kept in 'git stash list'.${NC}"
    fi
    [ "$UPDATED" = true ] || exit 1
fi

if [ "$UPDATED" = true ] || [ "$1" = "--force-install" ]; then
    echo -e "${BLUE}Installing cam_ir into the venv...${NC}"
    "$PIP" install -e . --upgrade --quiet
    echo -e "${GREEN}✓ Installed${NC}"
fi

if [ "$UPDATED" = true ]; then
    echo ""
    echo -e "${YELLOW}The config repo now points at a new cam_ir commit. Commit it with:${NC}"
    echo "  git -C \"$CONFIG_DIR\" add libs/cam_ir && git -C \"$CONFIG_DIR\" commit -m 'Update cam_ir'"
fi

echo -e "${GREEN}=== Done ===${NC}"
