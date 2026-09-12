(ns io.github.getcolors.compute-managed-gcs-backend
  (:require [clojure.string :as str] [io.github.getcolors.compute-gcs :as gcs]
            [io.github.getcolors.compute-coordinator :as coordinator]))
(def marker-key "_colors/backend-owner.json")
(defn- check [value message] (when-not value (throw (ex-info message {}))))
(defn lifecycle [opts action environment runner factory]
 (let [mode (get opts :gcs-bucket-mode "external")]
  (check (contains? #{"managed" "external"} mode) "invalid GCS bucket mode")
  (if (or (= "external" mode) (some #(or (true? (get opts (keyword % "dry-run"))) (contains? #{:build "build"} (get opts (keyword % "event")))) ["blue" "red" "green"]))
   {:status "skipped"}
   (let [bucket (:gcs-bucket opts) region (:gcs-region opts) project (:google-project opts) profile (:profile opts)
         _ (check (and (string? bucket) (re-matches #"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]" bucket)
                       (string? region) (re-matches #"[a-z][a-z0-9-]+" region)
                       (string? project) (re-matches #"[a-z][a-z0-9-]+" project)
                       (string? profile) (re-matches #"[a-z0-9][a-z0-9_-]{0,62}" profile)) "invalid managed GCS identity")
         request (gcs/client environment runner) path (gcs/bucket-path bucket)
         resolved (request "GET" (str "v1/projects/" project) nil {})
         _ (check (and (= project (:projectId resolved)) (string? (:projectNumber resolved)) (re-matches #"[1-9][0-9]*" (:projectNumber resolved))) "managed backend project verification failed")
         project-number (:projectNumber resolved)
         identity {:project project :bucket bucket :region region :profile profile}
         labels {:colors_profile profile :colors_project project :colors_purpose "managed-backend"}
         present (request "GET" path nil {}) owner (atom nil) deleting (atom false)]
    (if (and (nil? present) (or (= action "finalize") (some #(contains? #{:delete "delete"} (get opts (keyword % "event"))) ["blue" "red" "green"])))
     {:status "absent"}
     (try
      (let [metadata (or present
                        (do (check (not (true? (:compute-require-existing-state opts))) "existing managed backend required")
                            (let [created (request "POST" "storage/v1/b" {:name bucket :location region :labels labels
                              :iamConfiguration {:uniformBucketLevelAccess {:enabled true} :publicAccessPrevention "enforced"}
                              :versioning {:enabled true} :softDeletePolicy {:retentionDurationSeconds "0"}} {:project project})]
                             (check (= project-number (:projectNumber created)) "managed backend ownership mismatch")
                             (check (not (:conflict (gcs/put-object request bucket marker-key {:schema 1 :identity identity :status "active"} "0"))) "managed backend ownership conflict") created)))
            _ (check (and (= project-number (:projectNumber metadata)) (= labels (select-keys (:labels metadata) (keys labels))) (= region (str/lower-case (:location metadata)))) "managed backend ownership mismatch")
            observed (gcs/get-object request bucket marker-key)
            marker (or (:document observed) (when (and (= action "finalize") (= "deleting" (get-in metadata [:labels :colors_phase]))) {:schema 1 :identity identity :status "deleting"}))
            _ (check (and (= 1 (:schema marker)) (= identity (:identity marker)) (contains? #{"active" "deleting"} (:status marker))) "managed backend ownership mismatch")
            list-objects (fn [versions] (loop [page-token nil items []]
              (let [page (request "GET" (str path "/o") nil (cond-> {} versions (assoc :versions true) page-token (assoc :pageToken page-token))) items (into items (:items page))]
               (if (:nextPageToken page) (recur (:nextPageToken page) items) items))))]
       (if (= action "bootstrap")
        (do (check (and (= "active" (:status marker)) (not= "deleting" (get-in metadata [:labels :colors_phase]))) "managed backend deletion in progress")
            (check (not (:conflict (request "PATCH" path {:iamConfiguration {:uniformBucketLevelAccess {:enabled true} :publicAccessPrevention "enforced"}
                         :versioning {:enabled true} :softDeletePolicy {:retentionDurationSeconds "0"}} {:ifMetagenerationMatch (:metageneration metadata)}))) "managed backend metadata conflict")
            {:status "ready" :bucket bucket})
        (do
         (check (false? (:compute-prevent-destroy opts)) "managed backend deletion protected")
         (when (= "active" (:status marker))
          (reset! owner (factory opts environment)) (coordinator/acquire! @owner)
          (check (= "retired" (get-in (coordinator/snapshot @owner) [:document :status])) "compute must retire before backend deletion")
          (doseq [item (list-objects false) :let [key (:name item)] :when (not (contains? #{marker-key (str profile "/compute/coordination.json")} key))]
           (check (and (str/starts-with? key (str profile "/")) (str/ends-with? key ".tfstate/default.tfstate")) "managed backend contains unexpected objects")
           (let [state (:document (gcs/get-object request bucket key))]
            (check (and (= 4 (:version state)) (vector? (:resources state)) (not-any? #(seq (:instances %)) (:resources state))) "managed backend contains live state")))
          (check (not (:conflict (gcs/put-object request bucket marker-key (assoc marker :status "deleting") (:etag observed)))) "managed backend ownership conflict")
          (reset! deleting true))
         (check (not (:conflict (request "PATCH" path {:labels (assoc (:labels metadata) :colors_phase "deleting")} {:ifMetagenerationMatch (:metageneration metadata)}))) "managed backend metadata conflict")
         (doseq [item (sort-by #(= marker-key (:name %)) (list-objects true))]
          (check (not (:conflict (request "DELETE" (gcs/object-path bucket (:name item)) nil {:generation (:generation item) :ifGenerationMatch (:generation item)}))) "managed backend generation conflict"))
         (request "DELETE" path nil {}) {:status "destroyed"})))
      (finally (when (and @owner (not @deleting)) (coordinator/release! @owner)))))))))
