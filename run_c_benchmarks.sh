#!/bin/bash

sudo -v

# Experiment Runner Script
# Runs reduction experiments and logs results

# Configuration
BASE_DIR="C"
LOG_FILE="c_benchmark_results.csv"
FULL_PATH=$(pwd)

# Initialize CSV with headers
echo "folder_name,script_type,execution_time_seconds,initial_line_count,final_line_count,initial_tokens,final_tokens,status,timestamp" > "$LOG_FILE"

# Function to count lines in a file
count_lines() {
    wc -l < "$1" | tr -d ' '
}

# Function to count tokens using clang
count_tokens_clang() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "N/A"
        return
    fi
    # Use clang to tokenize and count (excludes whitespace and comments)
    local token_count=$(gcc -E -P "$file" 2>/dev/null | tr -s '[:space:]' '\n' | grep -v '^$' | wc -l)
    echo "$token_count"
}

# Function to log result
log_result() {
    local folder="$1"
    local script_type="$2"
    local exec_time="$3"
    local initial_line_count="${4:-N/A}"
    local final_line_count="${5:-N/A}"
    local initial_tokens="${6:-N/A}"
    local final_tokens="${7:-N/A}"
    local status="$8"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')

    echo "$folder,$script_type,$exec_time,$initial_line_count,$final_line_count,$initial_tokens,$final_tokens,$status,$timestamp" >> "$LOG_FILE"
}

# Function to run scythe with a specific mode
run_scythe() {
    local folder="$1"
    local mode="$2"
    sudo ./$BASE_DIR/$folder/test_r.sh $BASE_DIR/$folder/small.c
    sudo rm -f small.o

    echo "[$(date)] Running scythe --mode $mode for $folder"

    # Get initial counts before scythe
    local initial_line_count=$(count_lines "./$BASE_DIR/$folder/small.c")
    local initial_tokens=$(count_tokens_clang "./$BASE_DIR/$folder/small.c")

    local start_time=$(date +%s)
    nice -n 15 scythe --source-file "./$BASE_DIR/$folder/small.c" \
                                --script "./$BASE_DIR/$folder/test_r.sh" \
                                --language c \
                                --mode "$mode"
    local exit_code=$?
    local end_time=$(date +%s)
    local exec_time=$((end_time - start_time))

    # Count lines and tokens after reduction
    local final_line_count=$(count_lines "./$BASE_DIR/$folder/small.c")
    local final_tokens=$(count_tokens_clang "./$BASE_DIR/$folder/small.c")

    # Determine status
    local status="success"
    if [ $exit_code -ne 0 ]; then
        status="failed"
    fi

    # Log scythe result with initial and final counts
    log_result "$folder" "scythe_$mode" "$exec_time" "$initial_line_count" "$final_line_count" "$initial_tokens" "$final_tokens" "$status"

    # Run perses on the reduced file
    run_perses "$folder" "$mode"

    # Restore file after both scripts complete
    echo "[$(date)] Restoring small.c for $folder"
    git restore "./$BASE_DIR/$folder/small.c"
    sudo rm -f *.o

    return $exit_code
}

# Function to run perses
run_perses() {
    local folder="$1"
    local mode="$2"

    echo "[$(date)] Running perses on $mode file for $folder"

    # Check if the input file exists
    if [ ! -f "./$BASE_DIR/$folder/small.c" ]; then
        echo "[$(date)] Warning: small.c not found for $folder, skipping perses"
        return 1
    fi

    # Get initial counts before perses
    local initial_line_count=$(count_lines "$FULL_PATH/$BASE_DIR/$folder/small.c")
    local initial_tokens=$(count_tokens_clang "$FULL_PATH/$BASE_DIR/$folder/small.c")

    local start_time=$(date +%s)
    nice -n 19 sudo java -jar perses_deploy.jar \
                              --test-script "$FULL_PATH/$BASE_DIR/$folder/perses_r.sh" \
                              --input-file "$FULL_PATH/$BASE_DIR/$folder/small.c" \
                              -o ./perses_output
    local exit_code=$?
    local end_time=$(date +%s)
    local exec_time=$((end_time - start_time))

    # Count lines and tokens after perses reduction
    local final_line_count="N/A"
    local final_tokens="N/A"

    if [ -f "./perses_output/small.c" ]; then
        final_line_count=$(count_lines "./perses_output/small.c")
        final_tokens=$(count_tokens_clang "./perses_output/small.c")
    fi

    # Determine status
    local status="success"
    if [ $exit_code -ne 0 ]; then
        status="failed"
    fi

    # Log result with mode context
    log_result "$folder" "perses_after_$mode" "$exec_time" "$initial_line_count" "$final_line_count" "$initial_tokens" "$final_tokens" "$status"

    # Clean up perses output directory
    sudo rm -rf ./perses_output

    return $exit_code
}

# Function to process a single folder
process_folder() {
    local folder="$1"

    echo "=========================================="
    echo "[$(date)] Processing folder: $folder"
    echo "=========================================="

    # Check if test_r.sh exists
    if [ ! -f "./$BASE_DIR/$folder/test_r.sh" ]; then
        echo "[$(date)] Skipping $folder - test_r.sh not found"
        return
    fi

    # Check if small.c exists
    if [ ! -f "./$BASE_DIR/$folder/small.c" ]; then
        echo "[$(date)] Skipping $folder - small.c not found"
        return
    fi

    # Run baseline perses on original file first
    run_perses "$folder" "baseline"

#    # Run scythe with removal mode, then perses, then restore
    run_scythe "$folder" "removal"

#    # Run scythe with combination mode, then perses, then restore
    run_scythe "$folder" "combination"

#    # Run scythe with replacement mode, then perses, then restore
    run_scythe "$folder" "replacement"

    echo "[$(date)] Completed processing $folder"
    echo ""
}

# Main execution
main() {
    echo "Starting experiment runner at $(date)"
    echo "Base directory: $BASE_DIR"
    echo "Full path: $FULL_PATH"
    echo "Results will be saved to: $LOG_FILE"
    echo ""

    # Check if base directory exists
    if [ ! -d "$BASE_DIR" ]; then
        echo "Error: Directory $BASE_DIR not found!"
        exit 1
    fi

    # Process each folder in C directory
#    count=0
#    skip=5
#    limit=14
    # Define the skip list
    SKIP_LIST=(
        "clang-22704"
        "clang-23309"
        "clang-25900"
        "gcc-59903"
        "gcc-60116"
        "gcc-61383"
        "gcc-61917"
        "gcc-64990"
        "gcc-65383"
        "gcc-66186"
        "gcc-66375"
        "gcc-70127"
        "gcc-70586"
        "gcc-71626"
    )

    for folder in "$BASE_DIR"/*/ ; do
        sudo rm -f *.o
        sudo -v
        if [ -d "$folder" ]; then
#            count=$((count + 1))
#            if [ $count -le $skip ]; then
#              continue
#            fi
            folder_name=$(basename "$folder")

            # Skip if folder_name is in the skip list
            skip_folder=false
            for skip_item in "${SKIP_LIST[@]}"; do
                if [ "$folder_name" = "$skip_item" ]; then
                    skip_folder=true
                    break
                fi
            done
            if [ "$skip_folder" = true ]; then
                continue
            fi

            process_folder "$folder_name"
        fi
#        if [ $count -ge $limit ]; then
#            break
#        fi
    done

    echo "=========================================="
    echo "All experiments completed at $(date)"
    echo "Results saved to: $LOG_FILE"
    echo "=========================================="
}

# Run main function
main "$@"