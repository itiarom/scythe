# Scythe

Scythe is a semantic-aware program reducer that automatically minimizes source code while preserving specified properties. Using delta-debugging and language-specific reduction passes, it simplifies code for easier debugging and analysis across Solidity, C, and Java.

## Installation/Setup

**The project requires Python 3.10.14**

To install Scythe, clone the repository:

```bash
git clone https://github.com/chamitro/call_reducer.git
cd call_reducer
```

Install it in editable mode

```bash
pip install --editable .
```

## Usage

Scythe supports the following command-line arguments:

- `--language`: Specify the programming language. Options: `solidity`, `java`, or `c`. (Default: `"solidity"`)
- `--source-file`: The source file to minimize (rewritten in place).
- `--script`: The property test script (exit 0 iff the property holds).
- `--post-processor`: Optional. After scythe reaches a fixpoint, run `CMD <source-file> <script>` to reduce the file further in place (e.g., a Perses wrapper). Omitted = scythe behaves exactly as without it.

### Basic Command

```bash
scythe --source-file <file> --script <test-script> --language <lang>
```

## Solidity Setup

Scythe uses the Solidity compiler, and you can install multiple versions of it using solc-select. Follow these steps to install it:

```
#Install solc-select
pip3 install solc-select

#List available Solidity versions
solc-select install

#Install a specific Solidity version (e.g., version 0.8.0)
solc-select install 0.8.0
```
### Install Slither

Slither is a static analysis tool for Solidity smart contracts. You can install it as follows:

```
sudo python3 -m pip install slither-analyzer
```

Once installed, you can run Slither on a Solidity file, such as:

```
slither Solidity/smart2/original.sol
```

### Solidity Example Usage

To reduce a Solidity smart contract (e.g., `Solidity/smart2`), follow these steps. Scythe
minimizes its `--source-file` in place, so reduce a copy of `original.sol`:

```
# Select the required Solidity compiler version (see the benchmark's `version` file)
solc-select use "$(cat Solidity/smart2/version)"

# Work on a copy and (optionally) delete comments first
cp Solidity/smart2/original.sol /tmp/program.sol
python3 delete_comments.py --filepath /tmp/program.sol

# Run Scythe on the copy, using the benchmark's test script
scythe --source-file /tmp/program.sol --script ./Solidity/smart2/test.sh
```

Note: For each smart contract, ensure that Slither runs with the appropriate Solidity compiler version. The `solc-select use <version>` command is mandatory before running Slither.

Each benchmark under `Solidity/smart*/` contains exactly three files:

	- `original.sol`: the original (unminimized) smart contract.
	- `test.sh`: the property test script (runs Slither and checks the expected finding count).
	- `version`: the Solidity compiler version to use with `solc-select`.

## C Setup

Depending on the input script, Scythe uses multiple C compilers. In order to 
avoid using multiple versions of LLVM or GCC we recommend using docker
containers that contain the desired compiler version. In this project we provide
images for the following versions:

1. clang-3.5.0
2. clang-3.6.0
3. clang-3.7.0
4. clang-3.8.0
5. clang-7.1.0
6. gcc-4.8.0
7. gcc-4.8.2
8. gcc-4.9.0

The input script can then be modified to run the programs with the containerized
compiler.

If your script uses CompCert make sure to install the needed version.

### C Example Usage

To reduce a C program (e.g., `C/gcc-59903/original.c`) using the script `C/gcc-59903/test.sh`, follow these steps:

```
# Install the required compiler version, or build a docker container for the 
# version. If you choose to build a container the input script needs to be modified 

docker build -t <compiler version (e.g. gcc-4.8.0)>  --file <dockerfile path (e.g. ./dockerfiles/gcc_4_8.dockerfile)> .

# Run Scythe on the source file

scythe --source-file ./C/gcc-5990/original.c --script ./C/gcc-5990/test.sh --language c
```

Each C benchmark directory contains test scripts:
- `r.sh`: uses local versions of the required C compiler
- `test_r.sh`: uses docker containers with the required compiler installed

Install the CompCert compiler 3.7 version either using OPAM

```bash
# Install OPAM if needed, then:
opam install coq-compcert.3.7~coq-platform coq.8.11.0
```

Or from the source
```bash
tar xzf CompCert-3.7.tgz
cd CompCert-3.7
./configure x86_64-linux   # or arm-linux, etc.
make
make install
```

Build the following docker containers from the root directory (this may take some time):
   ```bash
    docker build -t clang-3.5.0  --file ./dockerfiles/clang_3_5_0.dockerfile .
    docker build -t clang-3.6.0  --file ./dockerfiles/clang_3_6_0.dockerfile .
    docker build -t clang-3.6.0  --file ./dockerfiles/clang_3_6_0.dockerfile .
    docker build -t clang-3.6.0-assertions  --file ./dockerfiles/clang_3_6_0_assertions.dockerfile .
    docker build -t clang-3.7.0  --file ./dockerfiles/clang_3_7_0.dockerfile .
    docker build -t clang-3.8.0  --file ./dockerfiles/clang_3_8_0.dockerfile .
    docker build -t clang-7.1.0  --file ./dockerfiles/clang_7_1_0.dockerfile .
    docker build -t gcc-4.8.2  --file ./dockerfiles/gcc_4_8_2.dockerfile .
    docker build -t gcc-4.8  --file ./dockerfiles/gcc_4_8.dockerfile .
    docker build -t gcc-4.9  --file ./dockerfiles/gcc_4_9.dockerfile .
   ```

You will also need to install the [Perses v2.5](https://github.com/uw-pluverse/perses)

## Java Setup

Before using the tool on Java programs, you need to ensure that Java is properly installed and configured. The project requires a compatible Java Development Kit (JDK) to compile and run Java programs.

Run the initial setup script first:

```
./java_evaluation_utils/setup.sh
```

### Java Example Usage

To reduce a Java program (e.g., `Java/jdk-iter_21/Main.java`) using the script `Java/jdk-iter_21/run.sh`, follow these steps:

```
# Run Scythe on the source file

scythe --source-file ./Java/jdk-iter_21/Main.java --script ./Java/jdk-iter_21/run.sh --language java
```

Each benchmark under `Java/jdk-iter_*/` or `Java/jdk-bugs-iter_*/` contains the following files:

	- `Main.java`: the original (unminimized) Java program.
	- `run.sh`: the property test script that verifies the program's behavior.

## Running Benchmarks

`./scripts/run-benchmarks.sh` runs three reduction methods per benchmark and writes the
minimized programs to an output directory (token counts / performance are measured separately).

### Running All Benchmarks

To run all benchmarks for a language:

```bash
# Run all benchmarks for a language
./scripts/run-benchmarks.sh -o output -l solidity
./scripts/run-benchmarks.sh -o output -l c
./scripts/run-benchmarks.sh -o output -l java
```

### Running Specific Benchmarks

```bash
# Run a single benchmark
./scripts/run-benchmarks.sh -o output -l solidity -b smart2
./scripts/run-benchmarks.sh -o output -l c -b clang-23309
./scripts/run-benchmarks.sh -o output -l java -b jdk-iter_21
```

### Running with Specific Reduction Methods

```bash
# Run only the Perses baseline
./scripts/run-benchmarks.sh -o output -l solidity --only-perses

# Run only scythe (+ Perses on scythe's output)
./scripts/run-benchmarks.sh -o output -l solidity --only-scythe
```

### Benchmark Output

For each benchmark `<name>` it produces:

```
output/<name>/minimized_scythe.<ext>          # scythe on the original
output/<name>/minimized_scythe_perses.<ext>   # Perses on scythe's output
output/<name>/minimized_perses.<ext>          # baseline: Perses on the original
output/<name>/time                            # one "method=seconds" line per method run
```

The workflow runs scythe on the original, then Perses on scythe's result
(`scythe+perses`), and separately Perses on the original (`perses` baseline). The
`scythe_perses` time is the sum of the scythe and Perses passes.
