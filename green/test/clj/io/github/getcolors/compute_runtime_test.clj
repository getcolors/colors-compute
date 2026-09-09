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

(deftest private-r2-session-preserves-ambient-aws-and-cleans
  (let [directory (atom nil)
        calls (atom [])
        env {"AWS_ACCESS_KEY_ID" "ambient-id" "AWS_SECRET_ACCESS_KEY" "ambient-secret"
             "AWS_PROFILE" "missing-profile" "AWS_DEFAULT_PROFILE" "missing-default"
             "HOME" "/operator/home" "COLORS_PAR_R2_ACCESS_KEY_ID" "backend-access-id"
             "COLORS_PAR_R2_SECRET_ACCESS_KEY" "backend-secret-value"
             "TF_CLI_ARGS" "-unsafe" "TF_LOG" "TRACE" "TF_WORKSPACE" "foreign"
             "TF_DATA_DIR" "/foreign/cache" "TOFU_LOG" "TRACE" "COLORS_PAR_UNUSED" "unused"}
        runner (fn [argv dir exact-env timeout]
                 (reset! directory dir)
                 (swap! calls conj argv)
                 (is (= 120000 timeout))
                 (is (= "rwx------" (mode dir)))
                 (is (= "rw-------" (mode (io/file dir "backend.tf.json"))))
                 (is (= "rw-------" (mode (io/file dir "credentials.tfbackend.json"))))
                 (is (= "ambient-id" (get exact-env "AWS_ACCESS_KEY_ID")))
                 (is (= "ambient-secret" (get exact-env "AWS_SECRET_ACCESS_KEY")))
                 (is (= "/operator/home" (get exact-env "HOME")))
                 (is (= "default" (get exact-env "TF_WORKSPACE")))
                 (is (not-any? #(contains? exact-env %) ["AWS_PROFILE" "AWS_DEFAULT_PROFILE" "TF_LOG" "TOFU_LOG" "TF_CLI_ARGS" "COLORS_PAR_UNUSED" "COLORS_PAR_R2_SECRET_ACCESS_KEY"]))
                 (is (= {"access_key" "backend-access-id" "secret_key" "backend-secret-value"}
                        (json/parse-string (slurp (io/file dir "credentials.tfbackend.json")))))
                 (is (not (clojure.string/includes? (pr-str argv) "backend-secret-value")))
                 (is (not (clojure.string/includes? (slurp (io/file dir "backend.tf.json")) "backend-secret-value")))
                 (if (= "init" (second argv))
                   (do (is (= ["tofu" "init" "-input=false" "-no-color" "-reconfigure"
                               (str "-backend-config=" (io/file dir "credentials.tfbackend.json"))] argv))
                       {:exit 0 :out "" :err ""})
                   (do (is (= ["tofu" "state" "pull"] argv))
                       {:exit 0 :out (json/generate-string valid-state) :err ""})))
        result (runtime/read-state r2 "example/compute/shared.tfstate" env runner)]
    (is (= {:status "present" :params {:provider "aws" :ip "192.0.2.1"}} result))
    (is (= 2 (count @calls)))
    (is (not (.exists (io/file @directory))))))

(defn read-fixture [text]
  (runtime/read-state s3 "example/state.tfstate" {}
    (fn [argv _ _ _] {:exit 0 :out (if (= "init" (second argv)) "" text)})))

(deftest unreadable-state-never-implies-absence
  (doseq [text ["" "null" "{}" "garbage" (str (json/generate-string valid-state) "{}")
                (str (json/generate-string valid-state) " trailing")]]
    (is (= {:status "error"} (read-fixture text))))
  (doseq [state [(assoc valid-state :version 4.5) (assoc valid-state :version true)
                 (assoc valid-state :serial -1) (assoc valid-state :serial 9007199254740992)
                 (assoc valid-state :serial false) (assoc valid-state :lineage " ")
                 (assoc valid-state :resources {}) (assoc valid-state :outputs [])
                 (assoc valid-state :outputs {:params nil})
                 (assoc valid-state :outputs {:params {:value []}})]]
    (is (= {:status "error"} (read-fixture (json/generate-string state)))))
  (is (= {:status "present" :params {}}
         (read-fixture (json/generate-string (assoc valid-state :outputs {}))))))

(deftest failures-and-secret-output-are-redacted
  (doseq [failure ["init" "state" "throw"]]
    (let [dir (atom nil)
          result (runtime/read-state s3 "example/state.tfstate" {}
                   (fn [argv directory _ _]
                     (reset! dir directory)
                     (if (= failure "throw") (throw (ex-info "SECRET" {}))
                         {:exit (if (= failure (second argv)) 1 0) :out "SECRET" :err "SECRET"})))]
      (is (= {:status "error"} result))
      (is (not (.exists (io/file @dir))))))
  (doseq [secret ["bound-secret-value" "secret-with-\"quote\\slash"]]
    (let [state (assoc-in valid-state [:outputs :params :value :leak] (str "prefix " secret " suffix"))]
    (is (= {:status "error"}
           (runtime/read-state r2 "example/state.tfstate"
             {"COLORS_PAR_R2_ACCESS_KEY_ID" "bound-id" "COLORS_PAR_R2_SECRET_ACCESS_KEY" secret}
             (fn [_ _ _ _] {:exit 0 :out (json/generate-string state)}))))))
  (doseq [secret [nil "" "REPLACE_ME" " "]]
    (is (= {:status "error"}
           (runtime/read-state r2 "example/state.tfstate"
             {"COLORS_PAR_R2_ACCESS_KEY_ID" secret "COLORS_PAR_R2_SECRET_ACCESS_KEY" "valid"}
             (fn [& _] (throw (AssertionError. "runner must not run"))))))))

(deftest cancellation-cleans-and-propagates
  (let [dir (atom nil)]
    (is (thrown? InterruptedException
                 (runtime/read-state s3 "example/state.tfstate" {}
                   (fn [_ directory _ _]
                     (reset! dir directory)
                     (throw (InterruptedException. "cancelled"))))))
    (is (not (.exists (io/file @dir))))))

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

(deftest mathematically-integral-json-numbers-are-valid
  (is (= {:status "present" :params {:provider "aws" :ip "192.0.2.1"}}
         (read-fixture (json/generate-string (assoc valid-state :version 4.0 :serial 0.0))))))

(deftest s3-preserves-ambient-profile-selection
  (is (= {:status "present" :params {:provider "aws" :ip "192.0.2.1"}}
         (runtime/read-state s3 "example/state.tfstate" {"AWS_PROFILE" "selected" "AWS_DEFAULT_PROFILE" "fallback"}
           (fn [_ _ env _]
             (is (= "selected" (get env "AWS_PROFILE")))
             (is (= "fallback" (get env "AWS_DEFAULT_PROFILE")))
             {:exit 0 :out (json/generate-string valid-state)})))))

(deftest native-runner-uses-only-supplied-path
  (let [directory (Files/createTempDirectory "colors-compute-path-test-" (make-array java.nio.file.attribute.FileAttribute 0))
        executable (.resolve directory "tofu")]
    (try
      (Files/createSymbolicLink executable (.toPath (io/file "/usr/bin/printf"))
                                (make-array java.nio.file.attribute.FileAttribute 0))
      (is (= {:exit 0 :out "exact-path" :err ""}
             (runtime/run-command ["tofu" "exact-path"] "/tmp" {"PATH" (str directory)} 1000)))
      (is (= -1 (:exit (runtime/run-command ["env"] "/tmp" {"PATH" (str directory)} 1000))))
      (finally (Files/deleteIfExists executable) (Files/deleteIfExists directory)))))

(deftest optional-flattened-outputs-refuse-sensitive-and-malformed-values
  (let [read (fn [document include?]
               (runtime/read-state s3 "example/compute/shared.tfstate" {}
                                   (fn [argv _ _ _] {:exit 0 :out (if (= "state" (second argv)) (json/generate-string document) "")}) include?))]
    (is (= {:status "present" :params {:provider "aws" :ip "192.0.2.1"}
            :outputs {:params {:provider "aws" :ip "192.0.2.1"} :subnet_id "subnet-1"} :state_empty false}
           (read (assoc-in valid-state [:outputs :subnet_id] {:value "subnet-1" :sensitive false}) true)))
    (doseq [bad [{:value "private" :sensitive true} {:sensitive false} {:value "private" :sensitive nil} "malformed"]]
      (let [document (assoc-in valid-state [:outputs :extra] bad)]
        (is (= "error" (:status (read document true))))
        (is (= "present" (:status (read document false))))))))
