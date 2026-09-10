(ns io.github.getcolors.compute-managed-backend
  "Owned S3 bootstrap and disposal after the entire deployment retires."
  (:require [cheshire.core :as json]
            [clojure.string :as str]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-coordinator :as coordinator])
  (:import [java.nio.file Files Path]
           [java.nio.file.attribute PosixFilePermissions FileAttribute]))
(def marker-key "_colors/backend-owner.json")
(defn- require-valid [condition message] (when-not condition (throw (ex-info message {}))))
(defn- lifecycle [opts action environment runner factory]
  (let [mode (get opts :s3-bucket-mode "external")]
    (require-valid (contains? #{"managed" "external"} mode) "invalid S3 bucket mode")
    (if (= mode "external") {:status "skipped"}
      (do
        (require-valid (= "s3" (:provider-backend opts)) "managed backend requires S3")
        (if (some #(or (true? (get opts (keyword % "dry-run"))) (contains? #{:build "build"} (get opts (keyword % "event")))) ["blue" "red" "green"])
          {:status "skipped"}
          (let [bucket (:s3-bucket opts) region (:s3-region opts) profile (:profile opts)
                _ (require-valid (and (string? bucket) (re-matches #"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]" bucket)
                                      (string? region) (re-matches #"[a-z]{2}(?:-[a-z]+)+-\d" region)
                                      (string? profile) (re-matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" profile)) "invalid managed backend identity")
                env (merge (into {} (remove (fn [[k _]] (re-find #"^(COLORS_PAR_|TF_|TOFU_)" k)) environment))
                           {"AWS_PAGER" "" "AWS_CLI_AUTO_PROMPT" "off" "AWS_MAX_ATTEMPTS" "1"})
                directory (Files/createTempDirectory "colors-backend-" (into-array FileAttribute [(PosixFilePermissions/asFileAttribute (PosixFilePermissions/fromString "rwx------"))]))
                owner (atom nil) deleting (atom false)]
            (try
              (letfn [(command [service operation args missing]
                        (let [result (runner (vec (concat ["aws" service operation] args ["--region" region "--output" "json" "--no-cli-pager"])) (str directory) env 120000)]
                          (if (zero? (:exit result)) (if (str/blank? (:out result)) {} (json/parse-string (:out result) true))
                              (let [code (second (re-find #"An error occurred \(([^()]+)\) when calling the " (:err result)))]
                                (if (contains? missing code) nil (throw (ex-info "managed backend AWS operation failed" {})))))))]
                (let [account (:Account (command "sts" "get-caller-identity" [] #{}))
                      _ (require-valid (and (string? account) (re-matches #"\d{12}" account)) "invalid AWS account")
                      identity {:account account :bucket bucket :region region :profile profile}
                      s3 (fn [operation args & [missing]] (command "s3api" operation (into ["--bucket" bucket "--expected-bucket-owner" account] args) (or missing #{})))
                      get-object (fn [key] (let [file (str (.resolve directory "read.json")) result (s3 "get-object" ["--key" key file] (if (= key marker-key) #{"NoSuchKey"} #{}))]
                                           {:document (when result (json/parse-string (slurp file) true)) :etag (:ETag result)}))
                      put-marker (fn [document etag] (let [file (.resolve directory "write.json")]
                                     (spit (str file) (json/generate-string document))
                                     (Files/setPosixFilePermissions file (PosixFilePermissions/fromString "rw-------"))
                                     (s3 "put-object" (into ["--key" marker-key "--body" (str file) "--content-type" "application/json"]
                                                           (if etag ["--if-match" etag] ["--if-none-match" "*"])))))
                      present (s3 "head-bucket" [] #{"404" "NoSuchBucket" "NotFound"})]
                  (if (and (nil? present) (or (= action "finalize") (some #(contains? #{:delete "delete"} (get opts (keyword % "event"))) ["blue" "red" "green"])))
                    {:status "absent"}
                    (do
                      (when (nil? present)
                        (require-valid (not (true? (:compute-require-existing-state opts))) "existing managed backend required")
                        (command "s3api" "create-bucket" (cond-> ["--bucket" bucket "--object-ownership" "BucketOwnerEnforced"]
                                                          (not= region "us-east-1") (into ["--create-bucket-configuration" (json/generate-string {:LocationConstraint region})])) #{})
                        (s3 "put-bucket-tagging" ["--tagging" (json/generate-string {:TagSet [{:Key "colors:profile" :Value profile} {:Key "colors:owner" :Value account} {:Key "colors:purpose" :Value "managed-backend"}]})])
                        (put-marker {:schema 1 :identity identity :status "active"} nil))
                      (let [tags (into {} (map (juxt :Key :Value) (:TagSet (s3 "get-bucket-tagging" []))))
                            _ (require-valid (and (= profile (get tags "colors:profile")) (= account (get tags "colors:owner")) (= "managed-backend" (get tags "colors:purpose"))) "managed backend ownership mismatch")
                            _ (require-valid (= region (or (:LocationConstraint (s3 "get-bucket-location" [])) "us-east-1")) "managed backend region mismatch")
                            {observed-marker :document etag :etag} (get-object marker-key)
                            marker (if (and (nil? observed-marker) (= action "finalize") (= "deleting" (get tags "colors:phase")))
                                     {:schema 1 :identity identity :status "deleting"} observed-marker)]
                        (require-valid (and (= 1 (:schema marker)) (= identity (:identity marker)) (contains? #{"active" "deleting"} (:status marker))) "managed backend ownership mismatch")
                        (if (= action "bootstrap")
                          (do
                            (require-valid (and (= "active" (:status marker)) (not= "deleting" (get tags "colors:phase"))) "managed backend deletion in progress")
                            (s3 "put-public-access-block" ["--public-access-block-configuration" (json/generate-string {:BlockPublicAcls true :IgnorePublicAcls true :BlockPublicPolicy true :RestrictPublicBuckets true})])
                            (s3 "put-bucket-encryption" ["--server-side-encryption-configuration" (json/generate-string {:Rules [{:ApplyServerSideEncryptionByDefault {:SSEAlgorithm "AES256"}}]})])
                            (s3 "put-bucket-versioning" ["--versioning-configuration" "{\"Status\":\"Enabled\"}"])
                            {:status "ready" :bucket bucket})
                          (do
                            (require-valid (false? (:compute-prevent-destroy opts)) "managed backend deletion protected")
                            (when (= "active" (:status marker))
                              (reset! owner (factory opts env))
                              (coordinator/acquire! @owner)
                              (require-valid (= "retired" (get-in (coordinator/snapshot @owner) [:document :status])) "compute must retire before backend deletion")
                              (doseq [item (:Contents (s3 "list-objects-v2" []))
                                      :let [key (:Key item)]
                                      :when (not (contains? #{marker-key (str profile "/compute/coordination.json")} key))]
                                (require-valid (and (str/starts-with? key (str profile "/")) (str/ends-with? key ".tfstate")) "managed backend contains unexpected objects")
                                (let [state (:document (get-object key))]
                                  (require-valid (and (= 4 (:version state)) (vector? (:resources state)) (not-any? #(seq (:instances %)) (:resources state))) "managed backend contains live state")))
                              (put-marker (assoc marker :status "deleting") etag)
                              (reset! deleting true))
                            (s3 "put-bucket-tagging" ["--tagging" (json/generate-string {:TagSet (mapv (fn [[k v]] {:Key k :Value v}) (assoc tags "colors:phase" "deleting"))})])
                            (loop []
                              (let [versions (s3 "list-object-versions" []) entries (concat (:Versions versions) (:DeleteMarkers versions))
                                    others (remove #(= marker-key (:Key %)) entries) batch (take 1000 (if (seq others) others entries))]
                                (when (seq batch)
                                  (let [result (s3 "delete-objects" ["--delete" (json/generate-string {:Objects (mapv #(select-keys % [:Key :VersionId]) batch) :Quiet true})])]
                                    (require-valid (empty? (:Errors result)) "managed backend version deletion failed"))
                                  (recur))))
                            (s3 "delete-bucket" []) {:status "destroyed"})))))))
              (finally
                (try (when (and @owner (not @deleting)) (coordinator/release! @owner))
                     (finally (with-open [paths (Files/walk directory (make-array java.nio.file.FileVisitOption 0))]
                                (doseq [path (reverse (sort-by #(.getNameCount ^Path %) (iterator-seq (.iterator paths))))] (Files/deleteIfExists ^Path path)))))))))))))
(defn bootstrap-backend!
  ([opts] (bootstrap-backend! opts (into {} (System/getenv))))
  ([opts environment] (bootstrap-backend! opts environment runtime/run-command))
  ([opts environment runner] (lifecycle opts "bootstrap" environment runner nil)))
(defn finalize-backend!
  ([opts] (finalize-backend! opts (into {} (System/getenv))))
  ([opts environment] (finalize-backend! opts environment runtime/run-command))
  ([opts environment runner] (finalize-backend! opts environment runner (fn [opts env] (coordinator/coordinator opts env nil nil nil {:event-prefix "lifecycle/"}))))
  ([opts environment runner factory] (lifecycle opts "finalize" environment runner factory)))
