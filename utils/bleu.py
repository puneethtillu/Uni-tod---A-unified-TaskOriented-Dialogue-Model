
# coding=utf-8
# Copyright 2017 The Tensor2Tensor Authors.
# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import subprocess
import tempfile
import logging

import numpy as np

import urllib.request

logger = logging.getLogger(__name__)


def get_moses_multi_bleu(hypotheses, references, lowercase=False):
    """Calculate the bleu score for hypotheses and references
    using the MOSES ulti-bleu.perl script.
    Args:
    hypotheses: A numpy array of strings where each string is a single example.
    references: A numpy array of strings where each string is a single example.
    lowercase: If true, pass the "-lc" flag to the multi-bleu script
    Returns:
    The BLEU score as a float32 value.
    """

    if isinstance(hypotheses, list):
        hypotheses = np.array(hypotheses)
    if isinstance(references, list):
        references = np.array(references)

    if np.size(hypotheses) == 0:
        return np.float32(0.0)

    # Get MOSES multi-bleu script
    #multi_bleu_path = os.path.join("", "multi-bleu.perl")
    try:
        multi_bleu_path, _ = urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/moses-smt/mosesdecoder/"
            "master/scripts/generic/multi-bleu.perl")
        os.chmod(multi_bleu_path, 0o755)
    except:
        print("Unable to fetch multi-bleu.perl script")
        return None

    # Dump hypotheses and references to tempfiles
    hypothesis_file = tempfile.NamedTemporaryFile()
    hypothesis_file.write("\n".join(hypotheses).encode("utf-8"))
    hypothesis_file.write(b"\n")
    hypothesis_file.flush()
    reference_file = tempfile.NamedTemporaryFile()
    reference_file.write("\n".join(references).encode("utf-8"))
    reference_file.write(b"\n")
    reference_file.flush()

    # Calculate BLEU using multi-bleu script
    with open(hypothesis_file.name, "r") as read_pred:
        bleu_cmd = [multi_bleu_path]
        if lowercase:
            bleu_cmd += ["-lc"]
        bleu_cmd += [reference_file.name]
        try:
            bleu_out = subprocess.check_output(bleu_cmd, stdin=read_pred, stderr=subprocess.STDOUT)
            bleu_out = bleu_out.decode("utf-8")
            bleu1 = re.search(r"BLEU = (.+?), (.+?)/", bleu_out).group(2)
            bleu2 = re.search(r"BLEU = (.+?), (.+?)/(.+?)/", bleu_out).group(3)
            bleu3 = re.search(r"BLEU = (.+?), (.+?)/(.+?)/(.+?)", bleu_out).group(4)
            bleu4 = re.search(r"BLEU = (.+?), (.+?)/(.+?)/(.+?)/(.+?)", bleu_out).group(5)
            bleu_score = re.search(r"BLEU = (.+?),", bleu_out).group(1)
            bleu_score = float(bleu_score)
            bleu_score = np.float32(bleu_score)
        except subprocess.CalledProcessError as error:
            if error.output is not None:
                print("multi-bleu.perl script returned non-zero exit code")
            bleu_score = None

    # Close temp files
    hypothesis_file.close()
    reference_file.close()
    if bleu_score is not None:
        return (float(bleu_score),float(bleu1),float(bleu2), float(bleu3), float(bleu4))
    else:
        return (0.0,0.0,0.0,0.0,0.0)

# def get_moses_multi_bleu(hypotheses, references, lowercase=False):
#     """Calculate the bleu score for hypotheses and references
#     using the MOSES ulti-bleu.perl script.
#     Args:
#     hypotheses: A numpy array of strings where each string is a single example.
#     references: A numpy array of strings where each string is a single example.
#     lowercase: If true, pass the "-lc" flag to the multi-bleu script
#     Returns:
#     The BLEU score as a float32 value.
#     """
#
#     if isinstance(hypotheses, list):
#         hypotheses = np.array(hypotheses)
#     if isinstance(references, list):
#         references = np.array(references)
#
#     if np.size(hypotheses) == 0:
#         return np.float32(0.0)
#
#     # Get MOSES multi-bleu script
#     try:
#         multi_bleu_path, _ = urllib.request.urlretrieve(
#             "https://raw.githubusercontent.com/moses-smt/mosesdecoder/"
#             "master/scripts/generic/multi-bleu.perl")
#         os.chmod(multi_bleu_path, 0o755)
#     except:
#         print("Unable to fetch multi-bleu.perl script")
#         return None
#
#     # Dump hypotheses and references to tempfiles
#     hypothesis_file = tempfile.NamedTemporaryFile()
#     hypothesis_file.write("\n".join(hypotheses).encode("utf-8"))
#     hypothesis_file.write(b"\n")
#     hypothesis_file.flush()
#     reference_file = tempfile.NamedTemporaryFile()
#     reference_file.write("\n".join(references).encode("utf-8"))
#     reference_file.write(b"\n")
#     reference_file.flush()
#
#     # Calculate BLEU using multi-bleu script
#     with open(hypothesis_file.name, "r") as read_pred:
#         bleu_cmd = [multi_bleu_path]
#         if lowercase:
#             bleu_cmd += ["-lc"]
#         bleu_cmd += [reference_file.name]
#         try:
#             bleu_out = subprocess.check_output(bleu_cmd, stdin=read_pred, stderr=subprocess.STDOUT)
#             bleu_out = bleu_out.decode("utf-8")
#             bleu1 = re.search(r"BLEU = (.+?), (.+?)/", bleu_out).group(2)
#             bleu2 = re.search(r"BLEU = (.+?), (.+?)/(.+?)/", bleu_out).group(3)
#             bleu3 = re.search(r"BLEU = (.+?), (.+?)/(.+?)/(.+?)", bleu_out).group(4)
#             bleu4 = re.search(r"BLEU = (.+?), (.+?)/(.+?)/(.+?)/(.+?)", bleu_out).group(5)
#             bleu_score = re.search(r"BLEU = (.+?),", bleu_out).group(1)
#             bleu_score = float(bleu_score)
#             bleu_score = np.float32(bleu_score)
#         except subprocess.CalledProcessError as error:
#             if error.output is not None:
#                 print("multi-bleu.perl script returned non-zero exit code")
#             bleu_score = None
#
#     # Close temp files
#     hypothesis_file.close()
#     reference_file.close()
#     if bleu_score is not None:
#         return (float(bleu_score),float(bleu1),float(bleu2), float(bleu3), float(bleu4))
#     else:
#         return (0.0,0.0,0.0,0.0,0.0)

if __name__ == '__main__':
    hypothesis =     ['the cat sat on the mat',
          'the brown fox jumps over the dog'
      ]
    reference =          [ 'the cat is on the mat',
          'a quick brown fox jumps over the lazy dog'
      ]

    print (get_moses_multi_bleu(hypothesis, reference, lowercase=True))