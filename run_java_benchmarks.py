import subprocess
import shutil
from pathlib import Path
from tqdm import tqdm
from datetime import datetime

base_dir = Path("Java/generator_modified")

# Number of iterations
num_iters = 21

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
results_dir = Path(f"java_evaluation_results_{timestamp}")
results_dir.mkdir(exist_ok=True)

print("Starting REMOVE MODE section")

setup_script = "java_evaluation_utils/setup.sh"
print(f"Running setup for remove mode: {setup_script}")
try:
    subprocess.run(["bash", str(setup_script)], check=True)
except subprocess.CalledProcessError as e:
    print(f"Setup script failed: {e}")
    exit(1)

removal_log_file = results_dir / "remove_mode_evaluation.log"

with open(removal_log_file, 'w') as removal_log:
    removal_log.write("=== REMOVE MODE EVALUATION LOG ===\n\n")

    for i in tqdm(range(1, num_iters + 1), desc="Running REMOVAL mode commands"):
        iter_dir = base_dir / f"iter_{i}"
        main_java = iter_dir / "Main.java"
        run_sh = iter_dir / "run.sh"

        command = [
            "scythe",
            "--source-file", str(main_java),
            "--script", str(run_sh),
            "--mode", "removal",
            "--language", "java",
        ]

        removal_log.write(f"\n{'=' * 50}\n")
        removal_log.write(f"ITERATION {i} - REMOVAL MODE\n")
        removal_log.write(f"Source: {main_java}\n")
        removal_log.write(f"Script: {run_sh}\n")
        removal_log.write(f"{'=' * 50}\n\n")
        removal_log.flush()

        try:
            subprocess.run(command, check=True, stdout=removal_log, stderr=subprocess.STDOUT)
            print(f"iter_{i} (removal) completed")
        except subprocess.CalledProcessError as e:
            print(f"\nError occurred in iter_{i} (removal): {e}")
            removal_log.write(f"\nERROR: {e}\n")
            removal_log.flush()

print("Saving REMOVAL mode results...")
removal_results_dir = results_dir / "removal_mode_results"
shutil.copytree(base_dir, removal_results_dir)
print(f"Removal mode results saved to: {removal_results_dir}")

cleanup_script = "java_evaluation_utils/cleanup.sh"
print(f"Running cleanup after removal mode: {cleanup_script}")
try:
    subprocess.run(["bash", str(cleanup_script)], check=True)
except subprocess.CalledProcessError as e:
    print(f"Cleanup script failed: {e}")

print("REMOVE MODE section completed\n")

# ===== REPLACEMENT MODE SECTION =====
print("Starting REPLACEMENT MODE section")

print(f"Running setup for replacement mode: {setup_script}")
try:
    subprocess.run(["bash", str(setup_script)], check=True)
except subprocess.CalledProcessError as e:
    print(f"Setup script failed: {e}")
    exit(1)

replacement_log_file = results_dir / "replace_mode_evaluation.log"

with open(replacement_log_file, 'w') as replacement_log:
    replacement_log.write("=== REPLACEMENT MODE EVALUATION LOG ===\n\n")

    for i in tqdm(range(1, num_iters + 1), desc="Running REPLACEMENT mode commands"):
        iter_dir = base_dir / f"iter_{i}"
        main_java = iter_dir / "Main.java"
        run_sh = iter_dir / "run.sh"

        command = [
            "scythe",
            "--source-file", str(main_java),
            "--script", str(run_sh),
            "--mode", "replacement",
            "--language", "java",
        ]

        replacement_log.write(f"\n{'=' * 50}\n")
        replacement_log.write(f"ITERATION {i} - REPLACEMENT MODE\n")
        replacement_log.write(f"Source: {main_java}\n")
        replacement_log.write(f"Script: {run_sh}\n")
        replacement_log.write(f"{'=' * 50}\n\n")
        replacement_log.flush()

        try:
            subprocess.run(command, check=True, stdout=replacement_log, stderr=subprocess.STDOUT)
            print(f"iter_{i} (replacement) completed")
        except subprocess.CalledProcessError as e:
            print(f"\nError occurred in iter_{i} (replacement): {e}")
            replacement_log.write(f"\nERROR: {e}\n")
            replacement_log.flush()

print("Saving REPLACEMENT mode results...")
replacement_results_dir = results_dir / "replacement_mode_results"
shutil.copytree(base_dir, replacement_results_dir)
print(f"Replacement mode results saved to: {replacement_results_dir}")

print(f"Running cleanup after replace mode: {cleanup_script}")
try:
    subprocess.run(["bash", str(cleanup_script)], check=True)
except subprocess.CalledProcessError as e:
    print(f"Cleanup script failed: {e}")

print("REPLACEMENT MODE section completed\n")

# ===== COMBINATION MODE SECTION =====
print("Starting COMBINATION MODE section")

print(f"Running setup for combination mode: {setup_script}")
try:
    subprocess.run(["bash", str(setup_script)], check=True)
except subprocess.CalledProcessError as e:
    print(f"Setup script failed: {e}")
    exit(1)

combination_log_file = results_dir / "combination_mode_evaluation.log"

with open(combination_log_file, 'w') as combination_log:
    combination_log.write("=== COMBINATION MODE EVALUATION LOG ===\n\n")

    for i in tqdm(range(1, num_iters + 1), desc="Running COMBINATION mode commands"):
        iter_dir = base_dir / f"iter_{i}"
        main_java = iter_dir / "Main.java"
        run_sh = iter_dir / "run.sh"

        command = [
            "scythe",
            "--source-file", str(main_java),
            "--script", str(run_sh),
            "--mode", "combination",
            "--language", "java",
        ]

        combination_log.write(f"\n{'=' * 50}\n")
        combination_log.write(f"ITERATION {i} - COMBINATION MODE\n")
        combination_log.write(f"Source: {main_java}\n")
        combination_log.write(f"Script: {run_sh}\n")
        combination_log.write(f"{'=' * 50}\n\n")
        combination_log.flush()

        try:
            subprocess.run(command, check=True, stdout=combination_log, stderr=subprocess.STDOUT)
            print(f"iter_{i} (combination) completed")
        except subprocess.CalledProcessError as e:
            print(f"\nError occurred in iter_{i} (combination): {e}")
            combination_log.write(f"\nERROR: {e}\n")
            combination_log.flush()

print("Saving COMBINATION mode results...")
combination_results_dir = results_dir / "combination_mode_results"
shutil.copytree(base_dir, combination_results_dir)
print(f"Removereplace mode results saved to: {combination_results_dir}")

print(f"Running cleanup after combination mode: {cleanup_script}")
try:
    subprocess.run(["bash", str(cleanup_script)], check=True)
except subprocess.CalledProcessError as e:
    print(f"Cleanup script failed: {e}")

print("COMBINATION MODE section completed\n")

# Final cleanup
print(f"Running final cleanup: {cleanup_script}")
try:
    subprocess.run(["bash", str(cleanup_script)], check=True)
except subprocess.CalledProcessError as e:
    print(f"Final cleanup script failed: {e}")

print("All evaluations finished!")
print(f"All results saved in: {results_dir}")
print(f"Removal mode log: {removal_log_file}")
print(f"Replacement mode log: {replacement_log_file}")
print(f"Combination mode log: {combination_log_file}")
print(f"Removal mode results: {removal_results_dir}")
print(f"Replacement mode results: {replacement_results_dir}")
print(f"Combination mode results: {combination_results_dir}")


