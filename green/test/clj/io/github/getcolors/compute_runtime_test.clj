(ns io.github.getcolors.compute-runtime-test
  (:require [clojure.test :refer [deftest is]]
            [clojure.java.io :as io]
            [cheshire.core :as json]
            [io.github.getcolors.compute-runtime :as runtime])
  (:import [java.nio.file Files LinkOption]
           [java.nio.file.attribute PosixFilePermissions]))

(def s3 {:provider-backend "s3" :s3-bucket "example" :s3-region "eu-west-1"})
(def r2 {:provider-backend "r2" :r2-bucket "example" :r2-endpoint "https://example.invalid"})
(def valid-state {:version 4 :serial 0 :lineage "example-lineage" :resources []
                  :outputs {:params {:value {:provider "aws" :ip "192.0.2.1"}}}})
(defn mode [file]
  (PosixFilePermissions/toString
   (Files/getPosixFilePermissions (.toPath (io/file file)) (make-array LinkOption 0))))

(deftest native-runner-replaces-the-environment
  (let [result (runtime/run-command ["/usr/bin/env"] "/tmp" {"ONLY_THIS" "present"} 1000)]
    (is (= 0 (:exit result)))
    (is (= "ONLY_THIS=present\n" (:out result)))))

(deftest native-runner-bounds-inherited-output-pipes
  (let [start (System/nanoTime)
        result (runtime/run-command
                ["/usr/bin/python3" "-c"
                 "import subprocess,time; subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(2)']); time.sleep(0.1)"]
                "/tmp" {} 250)
        elapsed (/ (- (System/nanoTime) start) 1e9)]
    (is (= -1 (:exit result)))
    (is (< elapsed 1.5))
    (is (= "" (:out result)))))

(deftest native-runner-uses-only-supplied-path
  (let [directory (Files/createTempDirectory "colors-compute-path-test-" (make-array java.nio.file.attribute.FileAttribute 0))
        executable (.resolve directory "tofu")]
    (try
      (spit (.toFile executable) "#!/bin/sh\nprintf '%s' \"$1\"\n")
      (.setExecutable (.toFile executable) true true)
      (is (= {:exit 0 :out "exact-path" :err ""}
             (runtime/run-command ["tofu" "exact-path"] "/tmp" {"PATH" (str directory)} 1000)))
      (is (= -1 (:exit (runtime/run-command ["env"] "/tmp" {"PATH" (str directory)} 1000))))
      (finally (Files/deleteIfExists executable) (Files/deleteIfExists directory)))))
