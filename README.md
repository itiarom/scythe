# Scythe

Scythe is a tool designed to minimize source code files by reducing their code, while keeping specific properties of the code. It supports both Solidity and C and is useful for simplifying code while retaining its functionality.

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

## Solidity Setup`

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

### Solidity Usage

Scythe supports the following arguments for solidity:

	- `--language`: Specify the programming language. Options: `solidity`, `java` or `c.``(Default: `"solidity"`)
	- `--source-file`: The source file to minimize.
	- `--script`: The script to run during the reduction process.

Each benchmark under `Solidity/smart*/` contains exactly three files:

	- `original.sol`: the original (unminimized) smart contract.
	- `test.sh`: the property test script (runs Slither and checks the expected finding count).
	- `version`: the Solidity compiler version to use with `solc-select`.

### Example Usage

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

### Running Solidity Benchmarks

`run_solidity_benchmarks.sh` runs three reduction methods per benchmark and writes the
minimized programs to an output directory (token counts / performance are measured separately):

```
# Run all benchmarks, writing results under ./output
./run_solidity_benchmarks.sh -o output

# Run a single benchmark
./run_solidity_benchmarks.sh -o output -b smart2

# Run only the Perses baseline, or only scythe (+ Perses on scythe's output)
./run_solidity_benchmarks.sh -o output --only-perses
./run_solidity_benchmarks.sh -o output --only-scythe
```

For each benchmark `<name>` it produces:

```
output/<name>/minimized_scythe.sol          # scythe on the original
output/<name>/minimized_scythe_perses.sol   # Perses on scythe's output
output/<name>/minimized_perses.sol          # baseline: Perses on the original
output/<name>/time                          # one "method=seconds" line per method run
```

The workflow runs scythe on the original, then Perses on scythe's result
(`scythe+perses`), and separately Perses on the original (`perses` baseline). The
`scythe_perses` time is the sum of the scythe and Perses passes.

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

### C Usage

Scythe supports the following arguments for C:

	- `--language`: Specify the programming language. Options: `solidity`, `java` or `c.``(Default: `"solidity"`)
	- `--source-file`: The source file to minimize. (Default: `"ext_changed.sol"`)
	- `--script`: The script to run during the reduction process. (Default: `"./solidity2.sh"`)
	- `--mode`: Only available for Java and C. The strategy to be followed by the reduction. Only accepts the values 'removal', 'replacement' and 'combination'

### Example Usage

To reduce a C program (e.g., `C/gcc-59903/small.c`) using the script `C/gcc-59903/r.sh`, follow these steps:

```
# Install the required compiler version, or build a docker container for the 
# version. If you choose to build a container the input script needs to be modified 

docker build -t <compiler version (e.g. gcc-4.8.0)>  --file <dockerfile path (e.g. ./dockerfiles/gcc_4_8.dockerfile)> .

# Run Scythe on the source file

scythe --source-file ./C/gcc-5990/small.c --script ./C/gcc-5990/r.sh --language c --mode removal
```

### Running C Benchmarks

In order to run the benchmarks locally you will need to follow the setup 
instructions.

In each C benchmark directory two script the files `r.sh` and `test_r.sh` can
be found. `r.sh` uses local versions of the required C compiler and `test_r.sh`
uses docker containers with the required compiler installed. The benchmark scipt
`./run_c_benchmarks.sh` uses `test_r.sh` as input.

#### C Benchmark setup

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

#### Running the C benchmarks

To run all benchmarks for C, execute:

```
./run_c_benchmarks.sh
```

In the root directory a CSV file containing the results will be created.

## Java Setup

Before using the tool on Java programs, run the initial setup script first.

```
./java_evaluation_utils/setup.sh
```

### Java Usage

Scythe supports the following arguments:

	- `--language`: Specify the programming language. Options: `solidity`, `java` or `c.``(Default: `"solidity"`)
	- `--source-file`: The source file to minimize. (Default: `"ext_changed.sol"`)
	- `--script`: The script to run during the reduction process. (Default: `"./solidity2.sh"`)
	- `--mode`: Only available for Java and C. The strategy to be followed by the reduction. Only accepts the values 'removal', 'replacement' and 'combination'

### Example Usage

To reduce a Java program (e.g., `Java/generator_modified/iter_1/Main.java`) using the script `Java/generator_modified/iter_1/run.sh`, follow these steps:

```
# Run Scythe on the source file

scythe --source-file ./Java/generator_modified/iter_1/Main.java --script ./Java/generator_modified/iter_1/run.sh --language java --mode removal
```

### Running Java Benchmarks

To run all benchmarks for Java, execute:

```
python run_java_benchmarks.py
```

A new directory with the name `java_evaluation_results_<timestamp>` will appear
containing the reduction results on the files found in `./Java`
