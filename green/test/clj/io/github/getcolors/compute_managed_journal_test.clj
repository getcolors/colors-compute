(ns io.github.getcolors.compute-managed-journal-test
 (:require [clojure.test :refer [deftest is testing]] [cheshire.core :as json] [io.github.getcolors.compute-managed-journal :as managed]))
(deftest shared-managed-contract
 (doseq [{:keys [name args expected error]} (json/parse-string-strict (slurp "../test/fixtures/managed-journal.json") true)]
  (testing name
   (if error (is (thrown-with-msg? Exception (re-pattern error) (apply managed/managed-coordination args)))
    (let [result (apply managed/managed-coordination args)] (is (= expected result)) (is (managed/managed-document-valid? (:document result))) (is (not (contains? (:document result) :nodes))) (is (not (contains? (:document result) :key))))))))
