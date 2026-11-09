<div id="top"></div>


<!-- PROJECT SHIELDS -->
[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![License][license-shield]][license-url]



<!-- PROJECT LOGO -->
<br />
<div align="center">
  <a href="https://github.com/AquariusDeFi/aqua-bribes">
    <img src="https://aqua.network/assets/img/header-logo.svg" alt="Logo" width="250" height="80">
  </a>

<h3 align="center">Aquarius Bribes</h3>

  <p align="center">
    Aquarius protocol is governed by DAO voting with AQUA tokens. Vote and participate in discussions to shape the future of Aquarius.
    <br />
    <br />
    <a href="https://github.com/AquariusDeFi/aqua-bribes/issues">Report Bug</a>
    ·
    <a href="https://vote.aqua.network/">Request Feature</a>
  </p>
</div>



<!-- TABLE OF CONTENTS -->
<details>
  <summary>Table of Contents</summary>
  <ol>
    <li>
      <a href="#about-the-project">About The Project</a>
      <ul>
        <li><a href="#built-with">Built With</a></li>
      </ul>
    </li>
    <li>
      <a href="#getting-started">Getting Started</a>
      <ul>
        <li><a href="#prerequisites">Prerequisites</a></li>
        <li><a href="#development-server">Development server</a></li>
        <li><a href="#running-tests">Running tests</a></li>
      </ul>
    </li>
    <li><a href="#contributing">Contributing</a></li>
    <li><a href="#contact">Contact</a></li>
  </ol>
</details>



<!-- ABOUT THE PROJECT -->
## About The Project

[![Aquarius voting tool Screen Shot][product-screenshot]](https://vote.aqua.network/)


#### What is Aquarius bribes?
The term bribe has been used in various DeFi communities for a while to describe the practice of buying votes.

Bribes offer a way for voters to earn incentives just by voting, with no need to place funds at risk inside of AMMs or order books. Essentially, bribes offer another utility benefit to AQUA holders.

We have brought bribes native to the Aquarius protocol, allowing everyone to view voting incentives all in one place. At the same time, through a simple interface, anyone can quickly set up and distribute bribe rewards on desired markets.

#### How does it work?
Using the interface, users allocate bribes to their chosen markets by selecting a market, the size of rewards, and the time period for the bribe.

From a technical standpoint, creating a bribe initiates a new claimable balance payment. The claimable balance has two Stellar wallets as claimants. One of them is the Aquarius bribe collection address, with the second being the indicator towards which market the bribes inside the claimable balance get allocated. It is also possible to schedule bribes in advance (e.g., several weeks ahead). Such schedules are also enforced on-chain through the use of timestamp predicates.

Every week on Sunday, the Aquarius bribe collector will claim all available claimable balances for the coming week. The Aquarius bribe collection address distributes all collected bribes to voters through the following Monday-Sunday.

The flow for voting will stay mostly the same. If a user decides to vote for a market pair offering bribes, they should expect to get their share of voting rewards daily.

Multiple tokens can get assigned as bribes for a market. Since bribes get delivered through standard Stellar payments, a user can choose not to accept certain bribe tokens by not making a trustline to the token.


<p align="right">(<a href="#top">back to top</a>)</p>



### Built With

* [Python](https://python.org/)
* [Django](https://www.djangoproject.com/)
* [Django REST framework](https://www.django-rest-framework.org/)
* [Celery](https://docs.celeryq.dev/en/stable/getting-started/introduction.html)
* [Stellar SDK](https://pypi.org/project/stellar-sdk/)

<p align="right">(<a href="#top">back to top</a>)</p>



<!-- GETTING STARTED -->

## Getting Started

### Prerequisites
* Python 3.11 and [pipenv](https://pipenv.pypa.io/)
* PostgreSQL
* A message broker for Celery: RabbitMQ by default, override it with `CELERY_BROKER_URL`

### Development server
Project built using django framework, so setup is similar to generic django project.

#### Clone project
`git clone git@github.com:AquariusDeFi/aqua-bribes.git`

#### Create environment & install requirements
`pipenv sync --dev`

#### Setup environment variable
```
echo 'export DATABASE_URL="postgres://username:password@localhost/aquarius_bribes"' > .env
```

#### Migrate database
`pipenv run python manage.py migrate --noinput`

### Create superuser
`pipenv run python manage.py createsuperuser`

#### Run server
`pipenv run python manage.py runserver 0.0.0.0:8000`

#### Run celery beater (background scheduler)
`pipenv run celery -A aquarius_bribes.taskapp beat`

#### Run celery worker (background worker)
`pipenv run celery -A aquarius_bribes.taskapp worker`

#### Done
That's it. Admin panel as well as api will be available at 8000 port: `http://localhost:8000/admin/login/`

### Running tests
See [TESTING.md](TESTING.md) for the Docker-based test and lint commands.


<p align="right">(<a href="#top">back to top</a>)</p>


<!-- CONTRIBUTING -->
## Contributing

Contributions are what make the open source community such an amazing place to learn, inspire, and create. Any contributions you make are **greatly appreciated**.

If you have a suggestion that would make this better, please fork the repo and create a pull request. You can also simply open an issue with the tag "enhancement".
Don't forget to give the project a star! Thanks again!

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

<p align="right">(<a href="#top">back to top</a>)</p>



<!-- CONTACT -->
## Contact

Email: [hello@aqua.network](mailto:hello@aqua.network)
Telegram chat: [@aquarius_HOME](https://t.me/aquarius_HOME)
Telegram news: [@aqua_token](https://t.me/aqua_token)
Twitter: [@aqua_token](https://twitter.com/aqua_token)
GitHub: [@AquariusDeFi](https://github.com/AquariusDeFi)
Discord: [@Aquarius](https://discord.gg/sgzFscHp4C)
Reddit: [@AquariusAqua](https://www.reddit.com/r/AquariusAqua/)
Medium: [@aquarius-aqua](https://medium.com/aquarius-aqua)

Project Link: [https://github.com/AquariusDeFi/aqua-bribes](https://github.com/AquariusDeFi/aqua-bribes)

<p align="right">(<a href="#top">back to top</a>)</p>



<!-- MARKDOWN LINKS & IMAGES -->
<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->
[contributors-shield]: https://img.shields.io/github/contributors/AquariusDeFi/aqua-bribes.svg?style=for-the-badge
[contributors-url]: https://github.com/AquariusDeFi/aqua-bribes/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/AquariusDeFi/aqua-bribes.svg?style=for-the-badge
[forks-url]: https://github.com/AquariusDeFi/aqua-bribes/network/members
[stars-shield]: https://img.shields.io/github/stars/AquariusDeFi/aqua-bribes.svg?style=for-the-badge
[stars-url]: https://github.com/AquariusDeFi/aqua-bribes/stargazers
[issues-shield]: https://img.shields.io/github/issues/AquariusDeFi/aqua-bribes.svg?style=for-the-badge
[issues-url]: https://github.com/AquariusDeFi/aqua-bribes/issues
[license-shield]: https://img.shields.io/github/license/AquariusDeFi/aqua-bribes.svg?style=for-the-badge
[license-url]: https://github.com/AquariusDeFi/aqua-bribes/blob/master/LICENSE
[product-screenshot]: images/screenshot.png
