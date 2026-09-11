(ns io.github.getcolors.compute-gcs-test
 (:require [clojure.test :refer :all]
           [io.github.getcolors.compute :as compute]
           [io.github.getcolors.compute-gcs :as gcs]
           [io.github.getcolors.compute-managed-gcs-backend :as backend]
           [io.github.getcolors.compute-coordinator :as coordinator]))
(def opts {:provider-backend "gcs" :provider-compute "google" :gcs-bucket "demo-states" :gcs-region "us-central1" :google-project "demo-project" :profile "demo" :gcs-bucket-mode "managed" :compute-prevent-destroy false})
(deftest rendering
 (is (= {:gcs {:bucket "demo-states" :prefix "demo/shared.tfstate"}} (get-in (compute/backend-plan opts "demo/shared.tfstate") [:config :terraform :backend])))
 (is (some? (coordinator/coordinator opts {} (fn [] {:status "absent"}) nil nil {}))))
(deftest ownership
 (with-redefs [gcs/client (fn [& _] (fn [& _] {:labels {} :location "US-CENTRAL1"}))]
  (is (thrown-with-msg? Exception #"ownership mismatch" (backend/lifecycle opts "bootstrap" {} nil nil)))))
(deftest bootstrap-and-teardown
 (let [bucket (atom nil) marker (atom nil) deleted (atom []) created (atom 0)
       request (fn [method path body query]
        (cond
         (= method "DELETE") (do (swap! deleted conj [path query]) {})
         (= method "PATCH") (swap! bucket merge body)
         (= path "storage/v1/b") (do (swap! created inc) (reset! bucket (assoc body :metageneration "1")))
         (.startsWith path "upload/") (do (reset! marker body) {:generation "3"})
         (.endsWith path "/o") {:items (if (:versions query) [{:name "demo/shared.tfstate/default.tfstate" :generation "1"} {:name "demo/shared.tfstate/default.tfstate" :generation "2"} {:name backend/marker-key :generation "3"}] [{:name "demo/shared.tfstate/default.tfstate"}])}
         (.contains path "/o/") (if (= "media" (:alt query)) (if (.contains path "backend-owner") @marker {:version 4 :resources []}) (when @marker {:generation "1"}))
         :else @bucket))]
  (with-redefs [gcs/client (fn [& _] request) coordinator/acquire! (fn [& _]) coordinator/snapshot (fn [& _] {:document {:status "retired"}}) coordinator/release! (fn [& _])]
   (is (= {:status "ready" :bucket "demo-states"} (backend/lifecycle opts "bootstrap" {} nil nil)))
   (is (= {:status "ready" :bucket "demo-states"} (backend/lifecycle opts "bootstrap" {} nil nil)))
   (is (= 1 @created))
   (is (= true (get-in @bucket [:versioning :enabled])))
   (is (= {:status "destroyed"} (backend/lifecycle opts "finalize" {} nil (fn [& _] {}))))
   (is (= 4 (count @deleted)))
   (is (= "storage/v1/b/demo-states" (ffirst (reverse @deleted)))))))
(deftest state-presence
 (require 'io.github.getcolors.compute-execution)
 (with-redefs [gcs/client (fn [& _] (fn [method path body query]
                                   (is (= "storage/v1/b/demo-states/o/demo%2Fcompute%2Fshared.tfstate%2Fdefault.tfstate" path))
                                   {:generation "22"}))]
  (is (= {:status "present"} ((requiring-resolve 'io.github.getcolors.compute-execution/state-presence) opts "demo/compute/shared.tfstate" {} nil)))))
