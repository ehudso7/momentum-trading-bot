#!/bin/bash

echo "🚀 Momentum Trading Pro - App Store Submission Script"
echo "=================================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check prerequisites
check_prerequisites() {
    echo -e "${YELLOW}Checking prerequisites...${NC}"

    # Check for EAS CLI
    if ! command -v eas &> /dev/null; then
        echo -e "${RED}EAS CLI not found. Installing...${NC}"
        npm install -g eas-cli
    fi

    # Check for Expo CLI
    if ! command -v expo &> /dev/null; then
        echo -e "${RED}Expo CLI not found. Installing...${NC}"
        npm install -g expo-cli
    fi

    echo -e "${GREEN}Prerequisites checked ✓${NC}"
}

# Build iOS app
build_ios() {
    echo -e "${YELLOW}Building iOS app...${NC}"

    # Create iOS build
    eas build --platform ios --profile production --non-interactive

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}iOS build completed ✓${NC}"
        return 0
    else
        echo -e "${RED}iOS build failed ✗${NC}"
        return 1
    fi
}

# Build Android app
build_android() {
    echo -e "${YELLOW}Building Android app...${NC}"

    # Create Android build
    eas build --platform android --profile production --non-interactive

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}Android build completed ✓${NC}"
        return 0
    else
        echo -e "${RED}Android build failed ✗${NC}"
        return 1
    fi
}

# Submit to App Store
submit_ios() {
    echo -e "${YELLOW}Submitting to App Store...${NC}"

    # Submit iOS app
    eas submit --platform ios --profile production --non-interactive

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}iOS app submitted to App Store ✓${NC}"
        echo -e "${GREEN}Check App Store Connect for review status${NC}"
        return 0
    else
        echo -e "${RED}iOS submission failed ✗${NC}"
        return 1
    fi
}

# Submit to Google Play
submit_android() {
    echo -e "${YELLOW}Submitting to Google Play...${NC}"

    # Submit Android app
    eas submit --platform android --profile production --non-interactive

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}Android app submitted to Google Play ✓${NC}"
        echo -e "${GREEN}Check Google Play Console for review status${NC}"
        return 0
    else
        echo -e "${RED}Android submission failed ✗${NC}"
        return 1
    fi
}

# Main execution
main() {
    echo "Starting app store submission process..."
    echo ""

    # Check prerequisites
    check_prerequisites
    echo ""

    # Build apps
    echo "Building apps for production..."
    build_ios &
    ios_pid=$!

    build_android &
    android_pid=$!

    # Wait for builds to complete
    wait $ios_pid
    ios_result=$?

    wait $android_pid
    android_result=$?

    echo ""

    # Submit if builds successful
    if [ $ios_result -eq 0 ]; then
        submit_ios
    fi

    if [ $android_result -eq 0 ]; then
        submit_android
    fi

    echo ""
    echo -e "${GREEN}=================================================="
    echo -e "Submission process complete!"
    echo -e "==================================================${NC}"
    echo ""
    echo "Next steps:"
    echo "1. iOS: Check App Store Connect for review status"
    echo "2. Android: Check Google Play Console for review status"
    echo "3. Respond to any review feedback promptly"
    echo "4. Monitor crash reports and user feedback"
    echo ""
    echo -e "${YELLOW}App review typically takes:${NC}"
    echo "• iOS: 24-48 hours"
    echo "• Android: 2-3 hours"
    echo ""
    echo -e "${GREEN}Good luck with your app launch! 🎉${NC}"
}

# Run main function
main