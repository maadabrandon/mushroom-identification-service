from comet_ml import Experiment
from typing import Union

import torch
from loguru import logger
from tqdm import tqdm

from torch.nn import CrossEntropyLoss
from torch.optim import Adam, SGD, RMSprop
from torch.optim.optimizer import Optimizer
from torchmetrics.classification import MulticlassPrecision, MulticlassAccuracy, MulticlassRecall

from src.setup.config import settings
from src.setup.paths import TRAIN_DATA_DIR, VAL_DATA_DIR, MODELS_DIR
from src.feature_pipeline.data_preparation import make_dataset, get_num_classes
from src.training_pipeline.models import BaseCNN, BiggerCNN, DynamicCNN, ResNet, get_resnet

experiment = Experiment(
    api_key=settings.comet_api_key,
    project_name=settings.comet_project_name,
    workspace=settings.comet_workspace,
    log_code=False
)


def get_optimizer(
        model_fn: Union[BaseCNN, DynamicCNN, ResNet],
        optimizer_name: str,
        learning_rate: float,
        weight_decay: float | None,
        momentum: float | None
) -> Optimizer:
    """
    The function returns the required optimizer function, based on the entered
    specifications.

    Args: 
        model_fn: the model that is being trained

        optimizer_name: the function that will be used to search for the
                   global minimum of the loss function.

        learning_rate: the learning rate that is optimizer is using for 
                       its search.

        weight_decay: a regularization term that reduces the network's weights

        momentum: the momentum coefficient used during stochastic gradient descent (SGD)

    Raises:,
        NotImplementedError: The requested optimizer has not been implemented

    Returns:
        Optimizer: the optimizer that will be returned.
    """
    optimizers_and_likely_spellings = {
        ("adam", "Adam"): Adam(params=model_fn.parameters(), lr=learning_rate, weight_decay=weight_decay),
        ("sgd", "SGD"): SGD(params=model_fn.parameters(), lr=learning_rate, momentum=momentum,
                            weight_decay=weight_decay),
        ("rmsprop", "RMSprop", "RMSProp"): RMSprop(
            params=model_fn.parameters(), lr=learning_rate, weight_decay=weight_decay, momentum=momentum
        )
    }

    optimizer_for_each_spelling = {
        spelling: function for spellings, function in optimizers_and_likely_spellings.items() for spelling in spellings
    }

    if optimizer_name in optimizer_for_each_spelling.keys():
        return optimizer_for_each_spelling[optimizer_name]

    # Choose Adam by default
    elif optimizer_name is None:
        return optimizer_for_each_spelling["adam"]

    else:
        raise NotImplementedError("Consider using the Adam, SGD, or RMSprop optimizers")


def run_training_loop(
        model_fn: Union[BaseCNN, DynamicCNN, ResNet],
        criterion: callable,
        save: bool,
        optimizer: callable,
        num_epochs: int,
        batch_size: int
) -> list[float]:
    """
    Initialise the multi-class precision, recall, and accuracy metrics.
    Then load the training data and set the training device. Train the 
    network in question for the specified number of epochs, put the 
    model in evaluation mode and report the average values of the
    validation loss, recall, accuracy, and precision

    Args:
        model_fn: the model object that is to be trained
        criterion: the loss function to be used 
        save: whether the model is to be saved

        optimizer: the optimizer that we will use to seek the global
                minimum of the loss function
                   
        num_epochs: the number of epochs that the model should be
                    trained for.

        batch_size: the number of data samples in each batch

    Returns:
        val_metrics: a list of floats which are the average values
                     of the loss, recall, accuracy, and precision
                     of the trained model on the validation set.             
    """
    # Prepare metrics

    num_classes = get_num_classes()
    precision = MulticlassPrecision(num_classes=num_classes, average="macro")
    recall = MulticlassRecall(num_classes=num_classes, average="macro")
    accuracy = MulticlassAccuracy(num_classes=num_classes, average="macro")

    logger.info("Collecting training data")
    train_loader = make_dataset(path=TRAIN_DATA_DIR, batch_size=batch_size)
    train_iterator = iter(train_loader)

    logger.info("Setting training device")

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model_fn.to(device=device)

    logger.info("Training the untuned model")
    for epoch in range(num_epochs):

        logger.info(f"Starting Epoch {epoch}")

        # Put model in training mode
        model_fn.train()

        # Initialise training loss
        training_loss_total = 0.0

        for (images, labels) in tqdm(train_loader):
            # Refresh gradients
            optimizer.zero_grad()

            images, labels = images.to(device), labels.to(device)
            output = model_fn.forward(images)

            # Calculate the training loss 
            training_loss = criterion(output, labels)

            # Calculate the gradient of the loss function
            training_loss.backward()

            # Adjust weights and biases
            optimizer.step()

            training_loss_total += training_loss.item()

        training_loss_avg = training_loss_total / len(train_iterator)

        # Put the model in evaluation mode
        model_fn.eval()

        # Initialise validation loss
        val_loss_total = 0.0
        val_recall_total = 0.0
        val_accuracy_total = 0.0
        val_precision_total = 0.0

        # Get validation data
        val_data_loader = make_dataset(path=VAL_DATA_DIR, batch_size=batch_size)
        val_iterator = iter(val_data_loader)

        with torch.no_grad():

            for (images, labels) in val_data_loader:
                images, labels = images.to(torch.device(device)), labels.to(torch.device(device))

                output = model_fn.forward(images)
                val_loss = criterion(output, labels).item()
                val_loss_total += val_loss

                _, predictions = torch.max(input=output, dim=1)

                val_recall = recall(predictions, labels)
                val_accuracy = accuracy(predictions, labels)
                val_precision = precision(predictions, labels)

                val_recall_total += val_recall
                val_accuracy_total += val_accuracy
                val_precision_total += val_precision

            val_loss_avg = val_loss_total / len(val_iterator)
            val_recall_avg = val_recall_total / len(val_iterator)
            val_accuracy_avg = val_accuracy_total / len(val_iterator)
            val_precision_avg = val_precision_total / len(val_iterator)

            logger.success(
                "Epoch: [{}/{}], Average Training Loss: {:.2f}, Average Validation_loss: {:.2f}, \
                Average Validation Accuracy: {:.2f}, Average Validation Recall: {:.2f},\
                Average Validation Precision: {:.2f}".format(epoch + 1, num_epochs, training_loss_avg, val_loss_avg,
                                                             val_accuracy_avg, val_recall_avg, val_precision_avg)

            )

            val_metrics = {
                "Epoch": epoch,
                "Average Training Loss": training_loss_avg,
                "Average Validation_loss": val_loss_avg,
                "Average Validation Accuracy": val_accuracy_avg,
                "Average Validation Recall": val_recall_avg,
                "Average Validation Precision": val_precision_avg
            }

            with experiment.test():
                experiment.log_metrics(val_metrics)

            experiment.log_confusion_matrix(
                y_true=labels,
                y_predicted=predictions,
                title="Confusion Matrix: Evaluation",
                file_name="confusion-matrix.json"
            )

    # Save model parameters
    if save:
        torch.save(model_fn.state_dict(), MODELS_DIR)

    logger.info("Finished Training")

    return [val_loss_avg, val_accuracy_avg, val_loss_avg, val_precision_avg]


def train(
        model_name: str,
        batch_size: int,
        learning_rate: float | None,
        weight_decay: float | None,
        momentum: float | None,
        dropout_prob: float | None,
        num_epochs: int,
        optimizer_name: str | None,
        tune_hyperparams: bool | None = True,
        tuning_trials: int | None = 10):
    """
    Train the requested model in either an untuned default state, or in the
    most optimal tuned form that was obtained after the specified number of 
    tuning trials.

    Args:
        model_name: the name of the model to be trained
        
        batch_size: the batch size to be used during training.

        learning_rate: the learning rate of the optimizer.

        weight_decay: a regularization term that reduces the weights

        num_epochs: the number of epochs that the model should be trained
                    for.

        dropout_prob: the proportion of nodes that will be omitted.

        optimizer_name: the name of the optimizer that is to be used.

        momentum: the momentum coefficient used during stochastic gradient descent (SGD)

        tune_hyperparams: a boolean that indicates whether hyperparameters
                          are to be tuned or not. If it is False, a default
                          version of the model will be trained.

        tuning_trials: the number of optuna trials to run.
    """
    num_classes = get_num_classes()
    logger.info("Setting up neural network")

    if not tune_hyperparams:

        if model_name in ["base", "Base"]:

            model_fn = BaseCNN(num_classes=num_classes)

        elif model_name in ["dynamic", "Dynamic"]:

            # Provide a default configuration
            default_layer_config = [
                {"type": "conv", "out_channels": 8, "kernel_size": 3, "padding": 1, "pooling": True, "stride": 1},
                {"type": "conv", "out_channels": 16, "kernel_size": 3, "padding": 1, "pooling": True, "stride": 1}

            ]

            model_fn = DynamicCNN(
                in_channels=3,
                num_classes=num_classes,
                layer_configs=default_layer_config,
                dropout_prob=dropout_prob
            )

        elif model_name in ["bigger", "Bigger"]:

            model_fn = BiggerCNN(
                in_channels=3,
                num_classes=num_classes,
                tune_hyperparams=tune_hyperparams,
                trial=None
            )

        elif "resnet" or "Resnet" or "ResNet" in model_name:
            model_fn = get_resnet(model_name=model_name)

        else:
            raise Exception(
                'Please enter "base", "dynamic"(for the base and dynamic models respectively), or the name of a ResNet.'
            )

        criterion = CrossEntropyLoss()

        chosen_optimizer = get_optimizer(
            model_fn=model_fn,
            learning_rate=learning_rate,
            optimizer_name=optimizer_name,
            weight_decay=weight_decay,
            momentum=momentum
        )

        val_metrics = run_training_loop(
            model_fn=model_fn,
            num_epochs=num_epochs,
            criterion=criterion,
            optimizer=chosen_optimizer,
            batch_size=batch_size,
            save=True
        )

    else:

        from src.training_pipeline.hyperparameter_tuning import optimize_hyperparams

        logger.info("Finding optimal values of hyperparameters")

        optimize_hyperparams(
            model_name=model_name,
            tuning_trials=tuning_trials,
            batch_size=batch_size,
            experiment=experiment
        )


train(
    model_name="resnet50",
    batch_size=20,
    learning_rate=None,
    num_epochs=20,
    dropout_prob=None,
    optimizer_name=None,
    tune_hyperparams=True,
    weight_decay=None,
    momentum=None
)
