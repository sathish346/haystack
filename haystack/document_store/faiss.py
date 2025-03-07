import logging
from pathlib import Path
from typing import Union, List, Optional, Dict, Generator
from tqdm import tqdm
try:
    import faiss
except ImportError:
    faiss = None
import numpy as np

from haystack import Document
from haystack.document_store.sql import SQLDocumentStore
from haystack.retriever.base import BaseRetriever
from haystack.utils import get_batches_from_generator
from scipy.special import expit


logger = logging.getLogger(__name__)


class FAISSDocumentStore(SQLDocumentStore):
    """
    Document store for very large scale embedding based dense retrievers like the DPR.

    It implements the FAISS library(https://github.com/facebookresearch/faiss)
    to perform similarity search on vectors.

    The document text and meta-data (for filtering) are stored using the SQLDocumentStore, while
    the vector embeddings are indexed in a FAISS Index.

    """

    def __init__(
        self,
        sql_url: str = "sqlite:///",
        vector_dim: int = 768,
        faiss_index_factory_str: str = "Flat",
        faiss_index: Optional[faiss.swigfaiss.Index] = None,
        return_embedding: bool = False,
        update_existing_documents: bool = False,
        index: str = "document",
        similarity: str = "dot_product",
        embedding_field: str = "embedding",
        progress_bar: bool = True,
        **kwargs,
    ):
        """
        :param sql_url: SQL connection URL for database. It defaults to local file based SQLite DB. For large scale
                        deployment, Postgres is recommended.
        :param vector_dim: the embedding vector size.
        :param faiss_index_factory_str: Create a new FAISS index of the specified type.
                                        The type is determined from the given string following the conventions
                                        of the original FAISS index factory.
                                        Recommended options:
                                        - "Flat" (default): Best accuracy (= exact). Becomes slow and RAM intense for > 1 Mio docs.
                                        - "HNSW": Graph-based heuristic. If not further specified,
                                                  we use the following config:
                                                  HNSW64, efConstruction=80 and efSearch=20
                                        - "IVFx,Flat": Inverted Index. Replace x with the number of centroids aka nlist.
                                                          Rule of thumb: nlist = 10 * sqrt (num_docs) is a good starting point.
                                        For more details see:
                                        - Overview of indices https://github.com/facebookresearch/faiss/wiki/Faiss-indexes
                                        - Guideline for choosing an index https://github.com/facebookresearch/faiss/wiki/Guidelines-to-choose-an-index
                                        - FAISS Index factory https://github.com/facebookresearch/faiss/wiki/The-index-factory
                                        Benchmarks: XXX
        :param faiss_index: Pass an existing FAISS Index, i.e. an empty one that you configured manually
                            or one with docs that you used in Haystack before and want to load again.
        :param return_embedding: To return document embedding
        :param update_existing_documents: Whether to update any existing documents with the same ID when adding
                                          documents. When set as True, any document with an existing ID gets updated.
                                          If set to False, an error is raised if the document ID of the document being
                                          added already exists.
        :param index: Name of index in document store to use.
        :param similarity: The similarity function used to compare document vectors. 'dot_product' is the default sine it is
                   more performant with DPR embeddings. 'cosine' is recommended if you are using a Sentence BERT model.
        :param embedding_field: Name of field containing an embedding vector.
        :param progress_bar: Whether to show a tqdm progress bar or not.
                             Can be helpful to disable in production deployments to keep the logs clean.
        """
        self.vector_dim = vector_dim
        self.faiss_index_factory_str = faiss_index_factory_str
        self.faiss_indexes: Dict[str, faiss.swigfaiss.Index] = {}
        if faiss_index:
            self.faiss_indexes[index] = faiss_index
        else:
            self.faiss_indexes[index] = self._create_new_index(
                vector_dim=self.vector_dim, index_factory=faiss_index_factory_str, **kwargs
            )

        self.return_embedding = return_embedding
        self.embedding_field = embedding_field
        if similarity == "dot_product":
            self.similarity = similarity
        else:
            raise ValueError("The FAISS document store can currently only support dot_product similarity. "
                             "Please set similarity=\"dot_product\"")
        self.progress_bar = progress_bar

        super().__init__(
            url=sql_url,
            update_existing_documents=update_existing_documents,
            index=index
        )

    def _create_new_index(self, vector_dim: int, index_factory: str = "Flat", metric_type=faiss.METRIC_INNER_PRODUCT, **kwargs):
        if index_factory == "HNSW" and metric_type == faiss.METRIC_INNER_PRODUCT:
            # faiss index factory doesn't give the same results for HNSW IP, therefore direct init.
            # defaults here are similar to DPR codebase (good accuracy, but very high RAM consumption)
            n_links = kwargs.get("n_links", 64)
            index = faiss.IndexHNSWFlat(vector_dim, n_links, metric_type)
            index.hnsw.efSearch = kwargs.get("efSearch", 20)#20
            index.hnsw.efConstruction = kwargs.get("efConstruction", 80)#80
            if "ivf" in index_factory.lower():  # enable reconstruction of vectors for inverted index
                self.faiss_indexes[index].set_direct_map_type(faiss.DirectMap.Hashtable)

            logger.info(f"HNSW params: n_links: {n_links}, efSearch: {index.hnsw.efSearch}, efConstruction: {index.hnsw.efConstruction}")
        else:
            index = faiss.index_factory(vector_dim, index_factory, metric_type)
        return index

    def write_documents(
        self, documents: Union[List[dict], List[Document]], index: Optional[str] = None, batch_size: int = 10_000
    ):
        """
        Add new documents to the DocumentStore.

        :param documents: List of `Dicts` or List of `Documents`. If they already contain the embeddings, we'll index
                          them right away in FAISS. If not, you can later call update_embeddings() to create & index them.
        :param index: (SQL) index name for storing the docs and metadata
        :param batch_size: When working with large number of documents, batching can help reduce memory footprint.
        :return:
        """

        index = index or self.index
        if not self.faiss_indexes.get(index):
            self.faiss_indexes[index] = self._create_new_index(
                vector_dim=self.vector_dim, index_factory=self.faiss_index_factory_str
            )

        field_map = self._create_document_field_map()
        document_objects = [Document.from_dict(d, field_map=field_map) if isinstance(d, dict) else d for d in documents]

        add_vectors = False if document_objects[0].embedding is None else True

        if self.update_existing_documents and add_vectors:
            logger.warning("You have enabled `update_existing_documents` feature and "
                           "`FAISSDocumentStore` does not support update in existing `faiss_index`.\n"
                           "Please call `update_embeddings` method to repopulate `faiss_index`")

        vector_id = self.faiss_indexes[index].ntotal
        for i in range(0, len(document_objects), batch_size):
            if add_vectors:
                embeddings = [doc.embedding for doc in document_objects[i: i + batch_size]]
                embeddings_to_index = np.array(embeddings, dtype="float32")
                self.faiss_indexes[index].add(embeddings_to_index)

            docs_to_write_in_sql = []
            for doc in document_objects[i: i + batch_size]:
                meta = doc.meta
                if add_vectors:
                    meta["vector_id"] = vector_id
                    vector_id += 1
                docs_to_write_in_sql.append(doc)

            super(FAISSDocumentStore, self).write_documents(docs_to_write_in_sql, index=index)

    def _create_document_field_map(self) -> Dict:
        return {
            self.index: self.embedding_field,
        }

    def update_embeddings(
        self,
        retriever: BaseRetriever,
        index: Optional[str] = None,
        update_existing_embeddings: bool = True,
        filters: Optional[Dict[str, List[str]]] = None,
        batch_size: int = 10_000
    ):
        """
        Updates the embeddings in the the document store using the encoding model specified in the retriever.
        This can be useful if want to add or change the embeddings for your documents (e.g. after changing the retriever config).

        :param retriever: Retriever to use to get embeddings for text
        :param index: Index name for which embeddings are to be updated. If set to None, the default self.index is used.
        :param update_existing_embeddings: Whether to update existing embeddings of the documents. If set to False,
                                           only documents without embeddings are processed. This mode can be used for
                                           incremental updating of embeddings, wherein, only newly indexed documents
                                           get processed.
        :param filters: Optional filters to narrow down the documents for which embeddings are to be updated.
                        Example: {"name": ["some", "more"], "category": ["only_one"]}
        :param batch_size: When working with large number of documents, batching can help reduce memory footprint.
        :return: None
        """

        index = index or self.index
        if not self.faiss_indexes.get(index):
            raise ValueError("Couldn't find a FAISS index. Try to init the FAISSDocumentStore() again ...")

        document_count = self.get_document_count(index=index)
        if document_count == 0:
            logger.warning("Calling DocumentStore.update_embeddings() on an empty index")
            return

        logger.info(f"Updating embeddings for {document_count} docs...")
        vector_id = self.faiss_indexes[index].ntotal

        result = self._query(
            index=index,
            vector_ids=None,
            batch_size=batch_size,
            filters=filters,
            only_documents_without_embedding=not update_existing_embeddings
        )
        batched_documents = get_batches_from_generator(result, batch_size)
        with tqdm(total=document_count, disable=not self.progress_bar) as progress_bar:
            for document_batch in batched_documents:
                embeddings = retriever.embed_passages(document_batch)  # type: ignore
                assert len(document_batch) == len(embeddings)

                embeddings_to_index = np.array(embeddings, dtype="float32")
                self.faiss_indexes[index].add(embeddings_to_index)

                vector_id_map = {}
                for doc in document_batch:
                    vector_id_map[doc.id] = vector_id
                    vector_id += 1
                self.update_vector_ids(vector_id_map, index=index)
                progress_bar.update(batch_size)
        progress_bar.close()

    def get_all_documents(
        self,
        index: Optional[str] = None,
        filters: Optional[Dict[str, List[str]]] = None,
        return_embedding: Optional[bool] = None,
        batch_size: int = 10_000,
    ) -> List[Document]:
        result = self.get_all_documents_generator(
            index=index, filters=filters, return_embedding=return_embedding, batch_size=batch_size
        )
        documents = list(result)
        return documents

    def get_all_documents_generator(
        self,
        index: Optional[str] = None,
        filters: Optional[Dict[str, List[str]]] = None,
        return_embedding: Optional[bool] = None,
        batch_size: int = 10_000,
    ) -> Generator[Document, None, None]:
        """
        Get all documents from the document store. Under-the-hood, documents are fetched in batches from the
        document store and yielded as individual documents. This method can be used to iteratively process
        a large number of documents without having to load all documents in memory.

        :param index: Name of the index to get the documents from. If None, the
                      DocumentStore's default index (self.index) will be used.
        :param filters: Optional filters to narrow down the documents to return.
                        Example: {"name": ["some", "more"], "category": ["only_one"]}
        :param return_embedding: Whether to return the document embeddings.
        :param batch_size: When working with large number of documents, batching can help reduce memory footprint.
        """
        index = index or self.index
        documents = super(FAISSDocumentStore, self).get_all_documents_generator(
            index=index, filters=filters, batch_size=batch_size, return_embedding=False,
        )
        if return_embedding is None:
            return_embedding = self.return_embedding

        for doc in documents:
            if return_embedding:
                if doc.meta and doc.meta.get("vector_id") is not None:
                    doc.embedding = self.faiss_indexes[index].reconstruct(int(doc.meta["vector_id"]))
            yield doc

    def get_documents_by_id(
        self, ids: List[str], index: Optional[str] = None, batch_size: int = 10_000
    ) -> List[Document]:
        index = index or self.index
        documents = super(FAISSDocumentStore, self).get_documents_by_id(ids=ids, index=index)
        if self.return_embedding:
            for doc in documents:
                if doc.meta and doc.meta.get("vector_id") is not None:
                    doc.embedding = self.faiss_indexes[index].reconstruct(int(doc.meta["vector_id"]))
        return documents

    def train_index(
        self,
        documents: Optional[Union[List[dict], List[Document]]],
        embeddings: Optional[np.ndarray] = None,
        index: Optional[str] = None,
    ):
        """
        Some FAISS indices (e.g. IVF) require initial "training" on a sample of vectors before you can add your final vectors.
        The train vectors should come from the same distribution as your final ones.
        You can pass either documents (incl. embeddings) or just the plain embeddings that the index shall be trained on.

        :param documents: Documents (incl. the embeddings)
        :param embeddings: Plain embeddings
        :param index: Name of the index to train. If None, the DocumentStore's default index (self.index) will be used.
        :return: None
        """

        index = index or self.index
        if embeddings and documents:
            raise ValueError("Either pass `documents` or `embeddings`. You passed both.")
        if documents:
            document_objects = [Document.from_dict(d) if isinstance(d, dict) else d for d in documents]
            doc_embeddings = [doc.embedding for doc in document_objects]
            embeddings_for_train = np.array(doc_embeddings, dtype="float32")
            self.faiss_indexes[index].train(embeddings_for_train)
        if embeddings:
            self.faiss_indexes[index].train(embeddings)

    def delete_all_documents(self, index: Optional[str] = None, filters: Optional[Dict[str, List[str]]] = None):
        """
        Delete all documents from the document store.
        """
        if filters:
            logger.warning("Filters are not supported for deleting documents in FAISSDocumentStore.")
        index = index or self.index
        if index in self.faiss_indexes.keys():
            self.faiss_indexes[index].reset()
        super().delete_all_documents(index=index)

    def query_by_embedding(
        self,
        query_emb: np.ndarray,
        filters: Optional[Dict[str, List[str]]] = None,
        top_k: int = 10,
        index: Optional[str] = None,
        return_embedding: Optional[bool] = None
    ) -> List[Document]:
        """
        Find the document that is most similar to the provided `query_emb` by using a vector similarity metric.

        :param query_emb: Embedding of the query (e.g. gathered from DPR)
        :param filters: Optional filters to narrow down the search space.
                        Example: {"name": ["some", "more"], "category": ["only_one"]}
        :param top_k: How many documents to return
        :param index: Index name to query the document from.
        :param return_embedding: To return document embedding
        :return:
        """
        if filters:
            logger.warning("Query filters are not implemented for the FAISSDocumentStore.")

        index = index or self.index
        if not self.faiss_indexes.get(index):
            raise Exception(f"Index named '{index}' does not exists. Use 'update_embeddings()' to create an index.")

        if return_embedding is None:
            return_embedding = self.return_embedding

        query_emb = query_emb.reshape(1, -1).astype(np.float32)
        score_matrix, vector_id_matrix = self.faiss_indexes[index].search(query_emb, top_k)
        vector_ids_for_query = [str(vector_id) for vector_id in vector_id_matrix[0] if vector_id != -1]

        documents = self.get_documents_by_vector_ids(vector_ids_for_query, index=index)

        #assign query score to each document
        scores_for_vector_ids: Dict[str, float] = {str(v_id): s for v_id, s in zip(vector_id_matrix[0], score_matrix[0])}
        for doc in documents:
            doc.score = scores_for_vector_ids[doc.meta["vector_id"]]
            doc.probability = float(expit(np.asarray(doc.score / 100)))
            if return_embedding is True:
                doc.embedding = self.faiss_indexes[index].reconstruct(int(doc.meta["vector_id"]))

        return documents

    def save(self, file_path: Union[str, Path]):
        """
        Save FAISS Index to the specified file.

        :param file_path: Path to save to.
        :return: None
        """
        faiss.write_index(self.faiss_indexes[self.index], str(file_path))

    @classmethod
    def load(
        cls,
        faiss_file_path: Union[str, Path],
        sql_url: str,
        index: str,
    ):
        """
        Load a saved FAISS index from a file and connect to the SQL database.
        Note: In order to have a correct mapping from FAISS to SQL,
              make sure to use the same SQL DB that you used when calling `save()`.

        :param faiss_file_path: Stored FAISS index file. Can be created via calling `save()`
        :param sql_url: Connection string to the SQL database that contains your docs and metadata.
        :param index: Index name to load the FAISS index as. It must match the index name used for
                      when creating the FAISS index.
        :return:
        """
        """
        """
        faiss_index = faiss.read_index(str(faiss_file_path))
        return cls(
            faiss_index=faiss_index,
            sql_url=sql_url,
            vector_dim=faiss_index.d,
            index=index,
        )

