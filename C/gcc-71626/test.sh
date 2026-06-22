#!/usr/bin/env bash

set -o pipefail
set -o nounset

# need to configure this part
WHICH=0     # 0: gcc; 1: clang
GOODCOMP=1  # 0: doesn't compile; 1: compiles
BADCC=("docker run --rm --label scythe_$$ -v $(pwd):/work gcc-4.9 gcc -O3  /work/${1:-program.c} -o /work/t")
GOODCC=("gcc")
CFILE=$(pwd)/${1:-program.c}
TIMEOUTCC=30

rm -f out*.txt

#############################
# iterate over the good ones
#############################

for cc in "${GOODCC[@]}" ; do
  rm -f ./t ./out1.txt

  (timeout -s 9 $TIMEOUTCC $cc $CFILE > out1.txt 2>&1) >& /dev/null
  ret=$?

  if [ $GOODCOMP -eq 1 ] ; then # does compile
    if [ $ret -ne 0 ] ; then
      echo "exit 1"
      docker ps --filter "label=scythe_$$" --format "{{.ID}}" | xargs -r docker kill
      exit 1
    fi
  else # does not compile, so make sure it doesn't ICE
    if grep 'internal compiler error: ' out1.txt ||\
    grep 'PLEASE ATTACH THE FOLLOWING FILES TO THE BUG REPORT' out1.txt
    then
      echo "exit 2"
      docker ps --filter "label=scythe_$$" --format "{{.ID}}" | xargs -r docker kill
      exit 1
    fi
  fi
done

#############################
# iterate over the bad ones
#############################

for cc in "${BADCC[@]}" ; do
  rm -f ./t ./out2.txt

  (timeout -s 9 $TIMEOUTCC $cc > out2.txt 2>&1) >& /dev/null

  if [ $WHICH -eq 1 ] ; then # clang
    if ! grep 'PLEASE ATTACH THE FOLLOWING FILES TO THE BUG REPORT' out2.txt ||\
    ! grep 'The annotation should be until the most recent cached token' out2.txt ||\
    grep ':[0-9]*: error: ' out2.txt | grep -E -v 'error: expected'  #conflicting|error: declaration|error: variable'
    then
      echo "exit 3"
      docker ps --filter "label=scythe_$$" --format "{{.ID}}" | xargs -r docker kill
      exit 1
    fi
  else # gcc
    if ! grep 'internal compiler error: in output_constant_pool_2' out2.txt
    then
      echo "exit 4"
      docker ps --filter "label=scythe_$$" --format "{{.ID}}" | xargs -r docker kill
      exit 1
    fi
  fi
done

echo "here"
exit 0
